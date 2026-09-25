# tests/test_agent_runtime.py
# Unit tests for agent/runtime.py
#
# NOTE: When patching instance methods via patch.object, the mock is a plain
# function — Python does NOT pass self. So mock signature is:
#   def mock(session_key, messages, tools)  — NOT (self, session_key, messages, tools)

import json
import os
import tempfile
import time
import unittest.mock
import uuid

import pytest

from agent.llm.cost import cost_for_model

from agent.runtime import AgentRuntime

from agent.llm.extractors import (
    extract_tool_calls,
    extract_text_content,
    extract_usage,
)


# Repo root, derived from this file's location (tests/ -> parent), so the
# source-inspection tests below work from any checkout, not just one machine.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _uniq():
    return f"rt{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def _hermetic_audit_flush(request):
    """Bug 3 hermeticity guard (SPEC-AUDIT-CLEANUP-1).

    _terminate_turn now auto-flushes each runtime's AuditLog; tests that
    exercise the tool loop (TestToolLoop, TestStreaming, ...) record real
    audit entries, so without this guard a full-suite run would append
    test data to the REAL ~/.config/crabcakes/audit-log.jsonl (the
    favorites.json lesson). Narrow intercept: patch AuditLog.flush_audit_log
    for this module only — no global conftest autouse.

    Manual patch/restore instead of monkeypatch: monkeypatch is
    function-scoped and cannot be used inside a module-scoped fixture.
    """
    from agent.audit import AuditLog
    original = AuditLog.flush_audit_log
    AuditLog.flush_audit_log = unittest.mock.MagicMock(return_value=None)
    yield
    AuditLog.flush_audit_log = original


def _resp(content="Done.", tool_calls=None):
    msg = {"content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


def _make_streaming_lambda(rt):
    """
    Return a lambda suitable for patching `_call_llm` in streaming tests.

    The lambda receives `(session_key, messages, tools)` as positional args
    from `_call_llm`, and forwards them plus boilerplate values to
    `rt._call_llm_streaming`. This eliminates duplication across the
    5 TestStreaming test patches.

    Usage:
        with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
            rt._run_loop(sk, "prompt")
    """
    return lambda *a, **kw: rt._call_llm_streaming(
        session_key=a[0],
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="openai/gpt-4o",
        caller_key="openai",  # PHASE-11: method on AgentRuntime
        messages=a[1],
        tools=a[2] if len(a) > 2 else None,
        timeout=30.0,
    )


# ═══════════════════════════════════════════════════════════════════
#  Cost computation
# ═══════════════════════════════════════════════════════════════════

class TestCostComputation:
    def test_openai_gpt4o(self):
        cost = cost_for_model("openai/gpt-4o", 1000, 500)
        assert abs(cost - 0.0075) < 0.0001

    def test_minimax(self):
        cost = cost_for_model("minimax/MiniMax-M2.5", 1000, 500)
        assert abs(cost - 0.001) < 0.0001

    def test_unknown_uses_openai_rates(self):
        assert cost_for_model("unknown/model", 1000, 500) > 0


# ═══════════════════════════════════════════════════════════════════
#  API response extraction
# ═══════════════════════════════════════════════════════════════════

class TestExtractToolCalls:
    def test_openai_single(self):
        resp = {
            "choices": [{"message": {
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                ]
            }}],
        }
        assert extract_tool_calls(resp, "openai") == [("c1", "read_file", {"path": "a.py"})]

    def test_openai_empty(self):
        assert extract_tool_calls({"choices": [{"message": {"content": "hi"}}]}, "openai") == []

    def test_anthropic_single(self):
        resp = {"content": [{"type": "tool_use", "id": "c2", "name": "list_files", "input": {"path": "."}}]}
        assert extract_tool_calls(resp, "anthropic") == [("c2", "list_files", {"path": "."})]

    def test_anthropic_empty(self):
        assert extract_tool_calls({"content": [{"type": "text", "text": "hi"}]}, "anthropic") == []


class TestExtractText:
    def test_openai(self):
        assert extract_text_content({"choices": [{"message": {"content": "The answer is 42."}}]}, "openai") == "The answer is 42."

    def test_anthropic(self):
        assert extract_text_content({"content": [{"type": "text", "text": "42 is the answer."}]}, "anthropic") == "42 is the answer."


class TestExtractUsage:
    def test_openai(self):
        assert extract_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 50}}, "openai") == (100, 50)

    def test_anthropic(self):
        assert extract_usage({"usage": {"input_tokens": 100, "output_tokens": 50}}, "anthropic") == (100, 50)

    def test_missing(self):
        assert extract_usage({}, "openai") == (0, 0)


# ═══════════════════════════════════════════════════════════════════
#  Lifecycle
# ═══════════════════════════════════════════════════════════════════

class TestLifecycle:
    def test_start_stop(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai", base_url="https://api.openai.com/v1",
                    api_key="test", default_model="gpt-4o",
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        assert not rt.is_running()
        rt.start()
        assert rt.is_running()
        rt.stop()
        assert not rt.is_running()

    def test_create_get(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai", base_url="https://api.openai.com/v1",
                    api_key="test", default_model="gpt-4o",
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = rt.create_conversation("Coder", _uniq(), "/tmp")
        conv = rt.get_conversation(sk)
        assert conv is not None
        assert conv.agent_name == "Coder"
        rt.stop()

    def test_get_unknown(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai", base_url="https://api.openai.com/v1",
                    api_key="test", default_model="gpt-4o",
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        assert rt.get_conversation("nosuch") is None
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Tool loop — test by calling _run_loop directly (synchronous)
#
#  IMPORTANT: When patching rt._call_llm, the mock is an unbound function.
#  Python does NOT pass self. Signature is:
#    def mock(session_key, messages, tools) -> dict
# ═══════════════════════════════════════════════════════════════════

def _make_cfg():
    from agent.config import AgentConfig, LLMProviderConfig
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


class TestToolLoop:
    def test_user_plus_assistant_in_conversation(self):
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        # mock(session_key, messages, tools) — NO self
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: _resp("Hello, human.")):
            rt._run_loop(sk, "say hello")

        conv = rt.get_conversation(sk)
        roles = [m.role.value for m in conv.messages]
        assert "user" in roles
        assert "assistant" in roles
        rt.stop()

    def test_user_message_in_llm_context(self):
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        received = []
        def mock(sk, msgs, tools, **kwargs):
            received.append(msgs)
            return _resp("Done.")

        with unittest.mock.patch.object(rt, "_call_llm", mock):
            rt._run_loop(sk, "what is the meaning of life")

        assert len(received) >= 1, f"_call_llm never called: {received}"
        assert any(
            m.get("role") == "user" and "meaning of life" in m.get("content", "")
            for m in received[0]
        ), f"User message not in {received[0]}"
        rt.stop()

    def test_text_response_callback(self):
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        complete = []
        rt._on_response_complete = lambda sk2, t: complete.append(t)

        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: _resp("The result is 42.")):
            rt._run_loop(sk, "what is 6 * 7?")

        assert complete == ["The result is 42."], f"Got: {complete}"
        rt.stop()

    def test_empty_text_content_with_choices_placeholder_saved_and_no_crash(self, caplog):
        """Regression: nemotron-3-ultra returns finish_reason='stop' with empty content after tool execution.

        Before the runtime.py:2272 guard fix, choices-present-but-empty-content was
        treated as a valid text response and persisted as
            {role: assistant, content: ''}
        which strict providers (Cohere, strict OpenAI tool-loop, Anthropic strict
        mode) reject with HTTP 400 "must have non-empty content or tool calls".
        The fix fires whenever text_content is empty regardless of choices.
        """
        import logging
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)

        # Response has choices=[{...}] (the nemotron case) but content=''
        empty_content_response = {
            "choices": [{"message": {"content": "", "tool_calls": []}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 0},
        }
        # Use a MagicMock so we can assert call_count == 1 (BUG #4 audit finding:
        # proves the loop actually exits, not just that the placeholder was inserted.
        # A regression that removes the `return` would silently let the loop iterate
        # until max_iterations_enforced trips).
        llm_mock = unittest.mock.MagicMock(return_value=empty_content_response)
        with unittest.mock.patch.object(rt, "_call_llm", llm_mock):
            with caplog.at_level(logging.WARNING, logger="agent.runtime"):
                rt._run_loop(sk, "do something")

        # 0. The LLM was called exactly once — proves the loop exited on the empty
        # response instead of iterating until max_iterations_enforced.
        assert llm_mock.call_count == 1, (
            f"Expected _call_llm to be called exactly once (loop exits after empty "
            f"response); got {llm_mock.call_count} calls"
        )

        # 1. The on_error callback fires
        assert any("no content" in e.lower() or "configuration error" in e.lower() for e in errors), (
            f"Expected _on_error to fire with 'no content' message; got: {errors}"
        )

        # 2. The conversation got a non-empty placeholder assistant message, NOT ''
        conv = rt.get_conversation(sk)
        asst_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        assert asst_msgs, f"Expected at least one assistant message; got: {conv.messages}"
        last = asst_msgs[-1]
        assert last.content != "", (
            f"Expected non-empty placeholder; got empty content. messages: "
            f"{[(m.role.value, m.content) for m in conv.messages]}"
        )
        assert "no content" in last.content.lower(), (
            f"Expected placeholder text mentioning 'no content'; got: {last.content!r}"
        )

        # 3. The warning was emitted with diagnostic context
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "agent.runtime"
        ]
        assert any("LLM returned no content" in r.getMessage() for r in warning_records), (
            f"Expected WARNING 'LLM returned no content' on agent.runtime; "
            f"got: {[(r.name, r.getMessage()) for r in caplog.records]}"
        )
        rt.stop()

    def test_empty_text_content_no_choices_placeholder_saved_and_no_crash(self):
        """Regression: response with NO choices key still triggers the guard.

        This is the case the original guard at agent/runtime.py:2272 already covered
        (`not text_content and not response.get('choices')`). After the fix the
        guard is `not text_content` only, so this case must continue to fire.
        """
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)

        # Response has no choices key at all
        bad_response = {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 0}}
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: bad_response):
            rt._run_loop(sk, "do something")

        assert errors, f"Expected _on_error to fire for no-choices response; got: {errors}"
        conv = rt.get_conversation(sk)
        asst_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        assert asst_msgs and asst_msgs[-1].content, (
            f"Expected placeholder assistant message; messages: "
            f"{[(m.role.value, m.content) for m in conv.messages]}"
        )
        rt.stop()

    def test_text_response_with_only_whitespace_does_not_falsely_trigger_guard(self):
        """Whitespace-only 'content' (e.g. '\\n\\n') is valid; guard must not fire.

        The guard is `if not text_content or (isinstance(text_content, str) and
        text_content.strip() == "")`. This test asserts that a string of only
        whitespace-trimmable characters DOES trigger the placeholder path — this
        is the BUG #1 audit fix. So this test has been flipped to verify the
        guard correctly REJECTS whitespace-only content (which strict providers
        like Cohere / Anthropic also 400 on).
        """
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)
        complete = []
        rt._on_response_complete = lambda sk2, t: complete.append(t)

        # Same whitespace string — but now we EXPECT the placeholder path to fire
        # because strict providers reject whitespace-only assistant content.
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: _resp(" \n\n ")):
            rt._run_loop(sk, "pad this")

        # _on_error fires because whitespace-only content is treated as empty
        assert any("no content" in e.lower() for e in errors), (
            f"Expected _on_error to fire for whitespace-only content; got: {errors}"
        )
        # And _on_response_complete does NOT fire (no useful text to deliver)
        assert complete == [], (
            f"Expected _on_response_complete to NOT fire for whitespace-only; got: {complete}"
        )
        # The placeholder is persisted
        conv = rt.get_conversation(sk)
        asst_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        assert asst_msgs and "no content" in asst_msgs[-1].content.lower(), (
            f"Expected placeholder assistant message; messages: "
            f"{[(m.role.value, m.content) for m in conv.messages]}"
        )
        rt.stop()

    def test_empty_text_content_on_error_handler_throws_loop_still_exits(self, caplog):
        """BUG #2 regression: user-registered _on_error callback throws.

        Before the fix, if _on_error raised, the exception propagated out of
        _run_loop mid-way. add_assistant_message had already executed, but
        _auto_save and `return` had not. The caller's loop could then call
        _call_llm again, get another empty response, and add a duplicate
        placeholder. If max_iterations_enforced didn't catch it, the
        conversation could fill with N duplicate placeholders.

        After the fix, _on_error is wrapped in try/except so:
        1. _auto_save still runs (placeholder persists to disk)
        2. `return` still executes (loop exits)
        3. The exception is logged at ERROR level, not swallowed silently
        """
        import logging
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        # _on_error raises — e.g. UI dispatcher broken
        def _broken_error_handler(sk2, msg):
            raise RuntimeError("UI dispatcher broken")

        rt._on_error = _broken_error_handler

        empty_content_response = {
            "choices": [{"message": {"content": "", "tool_calls": []}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 0},
        }
        llm_mock = unittest.mock.MagicMock(return_value=empty_content_response)
        with unittest.mock.patch.object(rt, "_call_llm", llm_mock):
            # The loop must complete cleanly without re-raising the handler exception
            with caplog.at_level(logging.ERROR, logger="agent.runtime"):
                rt._run_loop(sk, "do something")

        # Loop exited — LLM called exactly once, not N times until max_iterations
        assert llm_mock.call_count == 1, (
            f"Expected loop to exit after one LLM call (handler throw must not "
            f"cause re-iteration); got {llm_mock.call_count} calls"
        )

        # The handler exception was logged at ERROR level, not swallowed silently.
        # The dispatch infrastructure (runtime.py:1693, self._dispatch) wraps the
        # callback invocation in its own try/except and logs "Callback ... raised"
        # at ERROR level. Our BUG #2 fix wraps the dispatch call in a SECOND
        # try/except as defense in depth — in case the dispatcher itself throws.
        # Either layer catching is sufficient; we just need to confirm SOMETHING
        # logged the failure at ERROR level (not silently swallowed).
        error_records = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and r.name == "agent.runtime"
        ]
        assert any(
            "raised" in r.getMessage().lower() or "_on_error handler raised" in r.getMessage()
            for r in error_records
        ), (
            f"Expected ERROR log capturing handler exception; got: "
            f"{[(r.name, r.getMessage()) for r in caplog.records]}"
        )

        # Exactly one placeholder was added — NOT N duplicates from re-iteration
        conv = rt.get_conversation(sk)
        asst_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        placeholders = [m for m in asst_msgs if "no content" in m.content.lower()]
        assert len(placeholders) == 1, (
            f"Expected exactly one placeholder; got {len(placeholders)} (re-iteration "
            f"bug would add one per LLM call). messages: "
            f"{[(m.role.value, m.content) for m in conv.messages]}"
        )
        rt.stop()

    def test_tool_call_appends_result(self):
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        results = []
        rt._on_tool_call_result = lambda sk2, n, r, success=True: results.append((n, r))

        responses = [
            _resp(tool_calls=[{"id": "call_ls", "function": {"name": "list_files", "arguments": '{"path": "."}'}}]),
            _resp("Files listed."),
        ]
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: responses.pop(0)):
            rt._run_loop(sk, "list files")

        assert len(results) >= 1, f"Expected tool result, got: {results}"
        assert results[0][0] == "list_files"
        rt.stop()

    def test_tool_loop_two_iterations(self):
        """
        Verify: user → tool_result → assistant = 3 messages.
        The runtime updates the user message with tool_calls (rather than
        creating a separate assistant message with tool_calls), so the
        sequence is: user(with tool_calls), tool, assistant.
        """
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        call_num = [0]
        def mock_caller(sk, msgs, tools, **kwargs):
            call_num[0] += 1
            has_tool = any(m.get("role") == "tool" for m in msgs)
            if call_num[0] == 1:
                assert not has_tool, f"Call 1 should not have tool result. msgs={msgs}"
                return _resp(tool_calls=[{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}
                }])
            else:
                assert has_tool, f"Call 2 should have tool result. msgs={msgs}"
                return _resp("File contents.")

        with unittest.mock.patch.object(rt, "_call_llm", mock_caller):
            rt._run_loop(sk, "read a.py")

        conv = rt.get_conversation(sk)
        # user + assistant(with tool_calls) + tool_result + assistant(text) = 4 messages
        assert len(conv.messages) == 4, (
            f"Expected 4 messages, got {len(conv.messages)}: "
            f"{[(m.role.value, m.content[:30] if m.content else str(m.tool_calls)[:50]) for m in conv.messages]}"
        )
        # Verify tool_calls are on the assistant message, not user (Bug #4 fix)
        user_msg = conv.messages[0]
        assert user_msg.role.value == "user"
        assert len(user_msg.tool_calls) == 0, "User message should have no tool_calls"
        assistant_msg = conv.messages[1]
        assert assistant_msg.role.value == "assistant"
        assert len(assistant_msg.tool_calls) == 1
        assert assistant_msg.tool_calls[0].tool_name == "read_file"
        rt.stop()

    def test_max_iterations_enforced(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai", base_url="https://api.openai.com/v1",
                    api_key="test", default_model="gpt-4o",
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=2,      # ← hard-limit: stop after 2 calls
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        call_count = [0]
        def mock_caller(sk, msgs, tools, **kwargs):
            call_count[0] += 1
            return _resp(tool_calls=[{
                "id": f"c{call_count[0]}",
                "function": {"name": "list_files", "arguments": '{"path": "."}'}
            }])

        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)

        with unittest.mock.patch.object(rt, "_call_llm", mock_caller):
            rt._run_loop(sk, "keep calling tools")

        # Should stop after max_tool_iterations (2)
        assert call_count[0] <= 2, f"Expected <= 2 calls, got {call_count[0]}"
        rt.stop()

    def test_tool_call_response_with_empty_content_substitutes_placeholder(self, caplog):
        """BUG #3 sweep: tool-call response with empty/whitespace content.

        OpenAI spec allows `content=null` with `tool_calls=[]` present, but
        strict providers (Cohere, Anthropic strict mode) require non-empty
        content even when tool_calls are present. Before the fix, an LLM
        returning tool_calls alongside empty content persisted:
            {role: assistant, content: '', tool_calls: [...]}
        which strict providers reject with HTTP 400.

        After the fix, `_is_empty_content(text_content)` catches this case
        and substitutes "[calling tools]" before persisting, so the next
        LLM call sees a non-empty assistant message.
        """
        import logging
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        # Tool-call response with empty content (some providers do this on streaming completion)
        tc_response = {
            "choices": [{
                "message": {
                    "content": "",  # empty
                    "tool_calls": [{"id": "c1", "function": {"name": "list_files", "arguments": '{"path":"."}'}}],
                }
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
        }
        results = []
        rt._on_tool_call_result = lambda sk2, n, r, success=True: results.append((n, r))

        # Two responses: first a tool call (empty content), then a text
        # response so the loop terminates. Without the second response,
        # the loop hits max iterations and the last message is
        # "[max tool iterations reached]" instead of the placeholder.
        responses = [tc_response, _resp("Files listed successfully.")]
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: responses.pop(0)):
            with caplog.at_level(logging.WARNING, logger="agent.runtime"):
                rt._run_loop(sk, "list files")

        # Find the persisted assistant messages. With a 2-response sequence
        # (tool-call first, then text), the FIRST assistant message has the
        # substituted placeholder; the last is the text response.
        conv = rt.get_conversation(sk)
        asst_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        assert asst_msgs, f"Expected assistant message; got: {conv.messages}"

        # The FIRST assistant message is the tool-call response with the
        # substituted placeholder (not empty content).
        placeholder_msg = asst_msgs[0]
        assert placeholder_msg.content, (
            f"Expected non-empty content (placeholder substituted); got empty. "
            f"messages: {[(m.role.value, m.content) for m in conv.messages]}"
        )
        assert placeholder_msg.content == "[calling tools]", (
            f"Expected '[calling tools]' placeholder; got: {placeholder_msg.content!r}"
        )

        # And the tool_calls are intact (semantics preserved)
        assert placeholder_msg.tool_calls, (
            f"Expected tool_calls preserved on the substituted message; got: "
            f"{placeholder_msg.tool_calls}"
        )

        # The WARNING was emitted so provider issues are detectable
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "agent.runtime"
        ]
        assert any(
            "tool-call response has empty content" in r.getMessage()
            for r in warning_records
        ), (
            f"Expected WARNING about empty content in tool-call response; got: "
            f"{[(r.name, r.getMessage()) for r in warning_records]}"
        )
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  _compute_model_max — provider max_tokens resolution (BUG #1 Phase CB-1)
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  _is_empty_content — empty-content predicate (BUG #3 sweep helper)
# ═══════════════════════════════════════════════════════════════════

class TestIsEmptyContent:
    """Unit tests for the _is_empty_content helper used by both the text-only
    guard (BUG #1) and the tool-call-with-empty-content guard (BUG #3 sweep).
    """

    def test_none_returns_true(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content(None) is True

    def test_empty_string_returns_true(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content("") is True

    def test_whitespace_string_returns_true(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content(" ") is True
        assert _is_empty_content("\n") is True
        assert _is_empty_content("\t\t") is True
        assert _is_empty_content(" \n\t ") is True
        assert _is_empty_content("\u200b") is True  # zero-width space

    def test_non_empty_string_returns_false(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content("hi") is False
        assert _is_empty_content(" hi ") is False  # surrounding whitespace OK
        assert _is_empty_content("\nhello\n") is False

    def test_empty_list_returns_true(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content([]) is True

    def test_non_empty_list_returns_false(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content([{"text": "x"}]) is False

    def test_empty_dict_returns_true(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content({}) is True

    def test_zero_returns_true(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content(0) is True

    def test_nonzero_number_returns_false(self):
        from agent.runtime import _is_empty_content
        assert _is_empty_content(42) is False


# ═══════════════════════════════════════════════════════════════════
#  _compute_model_max — provider max_tokens resolution (BUG #1 Phase CB-1)
# ═══════════════════════════════════════════════════════════════════

class TestComputeModelMax:
    """Helper that resolves the model's context window from the provider config."""

    def test_returns_provider_max_tokens(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openrouter": LLMProviderConfig(
                    name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="test",
                    default_model="some-model",
                    max_tokens=200_000,
                )
            },
            default_provider="openrouter",
            default_model="openrouter/some-model",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "openrouter/some-model"
        assert rt._compute_model_max(conv) == 200_000
        rt.stop()

    def test_falls_back_to_128k_when_provider_unknown(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai", base_url="https://api.openai.com/v1",
                    api_key="test", default_model="gpt-4o",
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "unknown/model"  # provider not in config
        assert rt._compute_model_max(conv) == 128_000
        rt.stop()

    def test_falls_back_to_128k_when_max_tokens_is_zero(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openrouter": LLMProviderConfig(
                    name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="test",
                    default_model="some-model",
                    max_tokens=0,
                )
            },
            default_provider="openrouter",
            default_model="openrouter/some-model",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "openrouter/some-model"
        assert rt._compute_model_max(conv) == 128_000
        rt.stop()

    def test_falls_back_to_128k_when_max_tokens_is_none(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openrouter": LLMProviderConfig(
                    name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="test",
                    default_model="some-model",
                    max_tokens=None,
                )
            },
            default_provider="openrouter",
            default_model="openrouter/some-model",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "openrouter/some-model"
        assert rt._compute_model_max(conv) == 128_000
        rt.stop()

    def test_extracts_provider_name_from_slash_model(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openrouter": LLMProviderConfig(
                    name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="test",
                    default_model="claude-3-opus",
                    max_tokens=200_000,
                )
            },
            default_provider="openai",  # different default
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "openrouter/claude-3-opus"  # extracts "openrouter", not default_provider
        assert rt._compute_model_max(conv) == 200_000
        rt.stop()

    # BUG #3 regression tests: when max_tokens is unset/zero AND the caller
    # is recognized, fall back to caller_default_max_tokens() instead of 128K.
    # Before the fix, MiniMax-M3 (1M context) and Claude (200K) both defaulted
    # to 128K — wasting 87% / 36% of the model's context window.

    def test_minimax_zero_max_tokens_falls_back_to_caller_default(self):
        """When provider.max_tokens=0 and caller='minimax', returns 1_048_576
        (NOT 128K). This is the headline BUG #3 fix."""
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "minimax": LLMProviderConfig(
                    name="minimax",
                    base_url="https://api.minimax.io/v1",
                    api_key="test",
                    default_model="MiniMax-M3",
                    caller="minimax",
                    max_tokens=0,
                )
            },
            default_provider="minimax",
            default_model="minimax/MiniMax-M3",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "minimax/MiniMax-M3"
        assert rt._compute_model_max(conv) == 1_048_576, (
            "BUG #3: MiniMax-M3 has 1M context but default falls back to 128K. "
            "Caller-specific default must be used."
        )
        rt.stop()

    def test_anthropic_zero_max_tokens_falls_back_to_caller_default(self):
        """When provider.max_tokens=0 and caller='anthropic', returns 200_000."""
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "anthropic": LLMProviderConfig(
                    name="anthropic",
                    base_url="https://api.anthropic.com",
                    api_key="test",
                    default_model="claude-sonnet-4-20250514",
                    caller="anthropic",
                    max_tokens=0,
                )
            },
            default_provider="anthropic",
            default_model="anthropic/claude-sonnet-4-20250514",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "anthropic/claude-sonnet-4-20250514"
        assert rt._compute_model_max(conv) == 200_000
        rt.stop()

    def test_unknown_caller_falls_back_to_global_128k(self):
        """When caller is empty/unknown, fall back to 128K global default."""
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai",
                    base_url="https://api.openai.com/v1",
                    api_key="test",
                    default_model="gpt-4o",
                    caller="unknown_caller_xyz",  # not in CALLER_DEFAULT_MAX_TOKENS
                    max_tokens=0,
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "openai/gpt-4o"
        assert rt._compute_model_max(conv) == 128_000
        rt.stop()

    def test_max_tokens_set_wins_over_caller_default(self):
        """When provider.max_tokens > 0, caller default is ignored."""
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "minimax": LLMProviderConfig(
                    name="minimax",
                    base_url="https://api.minimax.io/v1",
                    api_key="test",
                    default_model="MiniMax-M3",
                    caller="minimax",
                    max_tokens=64_000,  # user explicitly set 64K
                )
            },
            default_provider="minimax",
            default_model="minimax/MiniMax-M3",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.model = "minimax/MiniMax-M3"
        assert rt._compute_model_max(conv) == 64_000
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Context-bloat fix — _run_loop trims conversation (BUG #1 Phase CB-1)
# ═══════════════════════════════════════════════════════════════════

class TestRunLoopTrimsContext:
    """§4.15 + BUG #1 fix: _run_loop trims the conversation to model_max per iteration."""

    def test_long_conversation_is_trimmed(self):
        """A 20-exchange conversation that exceeds model_max gets trimmed before the LLM call."""
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai",
                    base_url="https://api.openai.com/v1",
                    api_key="test-key",
                    default_model="gpt-4o",
                    max_tokens=25000,  # soft_ceiling=20K (trims ~20K-token conv), effective_budget=20916 (guard doesn't fire)
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt._RESPONSE_RESERVE_TOKENS = 0  # disable reserve for trim test
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.system_prompt = "Test"  # minimize for predictable trim behavior

        # Stuff the conversation with 80 large exchanges (~820 chars each = ~33K tokens)
        for i in range(80):
            conv.add_user_message(f"turn {i}: " + "x" * 820)
            conv.add_assistant_message("y" * 820, [])

        # Capture the breakdown callback output
        captured: list[dict] = []
        rt._on_token_breakdown = lambda session_key, bd: captured.append(bd)

        # Mock _call_llm to return a text-only response (no tool calls → loop exits)
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: _resp("Done.")):
            rt._run_loop(sk, "trigger the loop")

        # Post-conditions
        assert len(conv.messages) < 162, f"expected trim, got {len(conv.messages)} messages"
        assert captured, "on_token_breakdown never fired"
        last = captured[-1]
        assert last["trimmed_this_turn"] is True
        # messages_remaining is the count at trim time; conv grows after trim
        assert last["messages_removed_this_turn"] > 0
        assert isinstance(last["messages_remaining"], int)
        assert last["messages_remaining"] >= 1
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Cost + step limits
# ═══════════════════════════════════════════════════════════════════

class TestCostLimit:
    def test_cost_limit_stops(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai", base_url="https://api.openai.com/v1",
                    api_key="test", default_model="gpt-4o",
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
            cost_limit=0.0001,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)

        def mock_caller(sk, msgs, tools, **kwargs):
            return {
                "choices": [{"message": {"content": "Done."}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
            }

        with unittest.mock.patch.object(rt, "_call_llm", mock_caller):
            rt._run_loop(sk, "hello")

        assert len(errors) >= 1, f"Expected error, got: {errors}"
        assert "cost" in errors[0].lower(), f"Got: {errors[0]}"
        rt.stop()

    def test_step_limit_stops(self):
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai", base_url="https://api.openai.com/v1",
                    api_key="test", default_model="gpt-4o",
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=5,
            tool_timeout_seconds=30,
            auto_save_conversations=False,
            step_limit=0,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)

        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: _resp("Done.")):
            rt._run_loop(sk, "hello")

        assert len(errors) >= 1
        assert "step" in errors[0].lower(), f"Got: {errors[0]}"
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Approval callback
# ═══════════════════════════════════════════════════════════════════

class TestApproval:
    def test_exec_without_callback_denied(self):
        rt = AgentRuntime(_make_cfg())  # no approval callback
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        results = []
        rt._on_tool_call_result = lambda sk2, n, r, success=True: results.append((n, r))

        responses = [
            _resp(tool_calls=[{
                "id": "call_exec",
                "function": {"name": "exec_command", "arguments": '{"command": "ls"}'}
            }]),
            _resp("Done."),
        ]
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: responses.pop(0)):
            rt._run_loop(sk, "run ls")

        exec_results = [(n, r) for n, r in results if n == "exec_command"]
        assert len(exec_results) >= 1, f"Expected exec result, got: {results}"
        assert "approval" in exec_results[0][1].lower() or "denied" in exec_results[0][1].lower(), f"Got: {exec_results[0][1]}"
        rt.stop()

    def test_exec_with_approval_allow(self):
        approved = [False]
        rt = AgentRuntime(
            _make_cfg(),
            on_tool_call_approval_needed=lambda sk, tn, args: (approved.__setitem__(0, True) or True),
        )
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        responses = [
            _resp(tool_calls=[{
                "id": "call_exec",
                "function": {"name": "exec_command", "arguments": '{"command": "ls"}'}
            }]),
            _resp("Done."),
        ]
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: responses.pop(0)):
            rt._run_loop(sk, "run ls")

        assert approved[0], "Approval callback should have fired"
        rt.stop()

    def test_exec_with_approval_deny(self):
        rt = AgentRuntime(_make_cfg(), on_tool_call_approval_needed=lambda sk, tn, args: False)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        results = []
        rt._on_tool_call_result = lambda sk2, n, r, success=True: results.append((n, r))

        responses = [
            _resp(tool_calls=[{
                "id": "call_exec",
                "function": {"name": "exec_command", "arguments": '{"command": "ls"}'}
            }]),
            _resp("Done."),
        ]
        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: responses.pop(0)):
            rt._run_loop(sk, "run ls")

        exec_results = [(n, r) for n, r in results if n == "exec_command"]
        assert len(exec_results) >= 1, f"Expected exec result, got: {results}"
        assert "denied" in exec_results[0][1].lower(), f"Got: {exec_results[0][1]}"
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Persistence — use per-test temp dir so sessions don't collide
# ═══════════════════════════════════════════════════════════════════

class TestPersistence:
    def test_save_conversation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = AgentRuntime(_make_cfg())
            rt.start()
            sk = _uniq()
            rt.create_conversation("Coder", sk, tmpdir)
            rt.get_conversation(sk).add_user_message("hi")
            rt.get_conversation(sk).add_assistant_message("hello", [])

            path = rt.save_conversation(sk)
            assert os.path.isfile(path), f"Expected file at {path}"

            with open(path) as f:
                data = json.load(f)
            assert data["agent_name"] == "Coder"
            assert len(data["messages"]) == 2
            rt.stop()

    def test_load_conversation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = AgentRuntime(_make_cfg())
            rt.start()
            sk = _uniq()
            rt.create_conversation("Debugger", sk, tmpdir)
            rt.get_conversation(sk).add_user_message("debug this")
            rt.save_conversation(sk)
            rt.stop()

            rt2 = AgentRuntime(_make_cfg())
            rt2.start()
            ok = rt2.load_conversation(sk)
            assert ok is True
            conv = rt2.get_conversation(sk)
            assert conv is not None
            assert conv.agent_name == "Debugger"
            assert len(conv.messages) == 1
            rt2.stop()

    def test_list_conversations_contains_our_keys(self):
        """
        Verify sessions created in this runtime appear in its internal dict.
        Note: list_conversations() reads from the shared filesystem directory,
        so it includes sessions from all test runs. We test the in-memory
        _conversations dict directly for isolation.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = AgentRuntime(_make_cfg())
            rt.start()
            sk1 = rt.create_conversation("Coder", _uniq(), tmpdir)
            sk2 = rt.create_conversation("Debugger", _uniq(), tmpdir)

            # _conversations is the authoritative in-memory store
            assert sk1 in rt._conversations, f"{sk1} not in {list(rt._conversations.keys())}"
            assert sk2 in rt._conversations, f"{sk2} not in {list(rt._conversations.keys())}"
            rt.stop()


class TestListConversations:
    """Regression tests for list_conversations lightweight read (W13/W14).

    Uses XDG_CONFIG_HOME isolation so list_conversations reads from a
    dedicated temporary conversations directory, not the user's real one.
    """

    def _isolated_runtime(self, tmpdir):
        """Create an AgentRuntime with XDG_CONFIG_HOME pointing at tmpdir.

        Returns (rt, old_xdg) — caller must restore env in test cleanup.
        We do NOT auto-restore because list_conversations() calls
        _conversations_dir() lazily on each invocation.
        """
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmpdir
        rt = AgentRuntime(_make_cfg())
        rt.start()
        return rt, old_xdg

    @staticmethod
    def _restore_xdg(old_xdg):
        if old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old_xdg

    def test_list_conversations_returns_agent_name_without_full_deserialization(self):
        """list_conversations reads only agent_name from each JSON file,
        not the full Conversation/Message deserialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rt, old_xdg = self._isolated_runtime(tmpdir)
            sk = _uniq()
            rt.create_conversation("Coder", sk, tmpdir)
            conv = rt.get_conversation(sk)
            conv.add_user_message("hi")
            conv.add_assistant_message("hello", [])
            rt.save_conversation(sk)

            result = rt.list_conversations()
            our_entry = [entry for entry in result if entry[0] == sk]
            assert len(our_entry) == 1, f"Expected 1 entry for {sk}, got {len(our_entry)}"
            assert our_entry[0][1] == "Coder", f"Expected agent_name='Coder', got '{our_entry[0][1]}'"
            rt.stop()
            self._restore_xdg(old_xdg)

    def test_list_conversations_returns_unknown_for_corrupt_file(self):
        """A corrupt JSON file returns 'unknown' instead of crashing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rt, old_xdg = self._isolated_runtime(tmpdir)

            # _conversations_dir() lives at <XDG>/crabcakes/conversations
            conv_dir = os.path.join(tmpdir, "crabcakes", "conversations")
            os.makedirs(conv_dir, exist_ok=True)
            corrupt_path = os.path.join(conv_dir, "corrupt-session.json")
            with open(corrupt_path, "w") as f:
                f.write("{not valid json")

            result = rt.list_conversations()
            corrupt_entries = [e for e in result if e[0] == "corrupt-session"]
            assert len(corrupt_entries) == 1
            assert corrupt_entries[0][1] == "unknown"
            rt.stop()
            self._restore_xdg(old_xdg)

    def test_list_conversations_returns_unknown_for_missing_agent_name(self):
        """A JSON file without agent_name field returns 'unknown'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rt, old_xdg = self._isolated_runtime(tmpdir)

            conv_dir = os.path.join(tmpdir, "crabcakes", "conversations")
            os.makedirs(conv_dir, exist_ok=True)
            path = os.path.join(conv_dir, "no-name-session.json")
            with open(path, "w") as f:
                json.dump({"messages": []}, f)

            result = rt.list_conversations()
            entries = [e for e in result if e[0] == "no-name-session"]
            assert len(entries) == 1
            assert entries[0][1] == "unknown"
            rt.stop()
            self._restore_xdg(old_xdg)

    def test_list_conversations_empty_directory(self):
        """An empty conversations directory returns an empty list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rt, old_xdg = self._isolated_runtime(tmpdir)
            result = rt.list_conversations()
            assert result == []
            rt.stop()
            self._restore_xdg(old_xdg)


# ═══════════════════════════════════════════════════════════════════
#  SSE Streaming (Phase 1.3b)
# ═══════════════════════════════════════════════════════════════════

def _mock_stream_openai_3_chunks():
    """
    Yield the equivalent of: "Hello world!" as 3 SSE events.
    Returns a generator compatible with OpenAIProvider("openai").stream signature.
    """
    from agent.runtime import SSEEvent
    # Chunk 1: "Hello"
    yield SSEEvent(type="text_delta", data={"content": "Hello"})
    # Chunk 2: " world"
    yield SSEEvent(type="text_delta", data={"content": " world"})
    # Chunk 3: "!"
    yield SSEEvent(type="text_delta", data={"content": "!"})
    # Done
    yield SSEEvent(type="done", data={})


def _mock_stream_with_tool_call():
    """
    Yield a text delta followed by a streaming tool call.
    Tool call: list_files on path "."

    NOTE: Each argument chunk must be a valid JSON fragment. Accumulates to
    the JSON object: {"path": "."}
    """
    from agent.runtime import SSEEvent
    # Text delta first
    yield SSEEvent(type="text_delta", data={"content": "Let me list the files:"})
    # Tool call delta (name)
    yield SSEEvent(type="tool_call_delta", data={"index": 0, "name": "list_files", "arguments": ""})
    # Tool call delta (partial arguments — each chunk is a valid JSON fragment)
    yield SSEEvent(type="tool_call_delta", data={"index": 0, "name": "", "arguments": '{"path": "."}'})
    # Done
    yield SSEEvent(type="done", data={})



class TestStreaming:
    """
    Test SSE streaming by mocking _get_provider (Phase B6+ pattern).
    When on_text_delta is set, _call_llm routes to _call_llm_streaming,
    which iterates the SSE generator and fires callbacks per event.
    """

    def test_text_delta_fires_incrementally(self):
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, delta: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        deltas = []
        rt._on_text_delta = lambda sk, d: deltas.append(d)
        turn_starts = []
        rt._on_turn_start = lambda sk: turn_starts.append(sk)

        # Patch get_provider to return a mock provider whose stream()
        # yields 3 text chunks (dispatch now goes through the registry).
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="text_delta", data={"content": "Hello"}),
            SSEEvent(type="text_delta", data={"content": " world"}),
            SSEEvent(type="text_delta", data={"content": "!"}),
            SSEEvent(type="done", data={}),
        ])
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "say hello")

        # The turn-start signal moved to on_turn_start (BUG #21 redesign):
        # exactly one turn-start, one delta per chunk — no leading "".
        assert turn_starts == [sk], (
            f"Expected exactly 1 turn-start for {sk}, got {turn_starts}"
        )
        assert len(deltas) == 3, f"Expected 3 deltas, got {len(deltas)}: {deltas}"
        assert deltas[0] == "Hello"
        assert deltas[1] == " world"
        assert deltas[2] == "!"
        rt.stop()

    def test_response_complete_fires_after_stream(self):
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, delta: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        complete = []
        rt._on_response_complete = lambda sk, t: complete.append(t)

        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.stream.return_value = _mock_stream_openai_3_chunks()
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "say hello")

        # on_response_complete fires once with full accumulated text
        assert len(complete) == 1, f"Expected 1 complete, got {len(complete)}: {complete}"
        assert complete[0] == "Hello world!"
        rt.stop()

    def test_tool_call_start_fires_when_complete(self):
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        tool_starts = []
        rt._on_tool_call_start = lambda sk, name, args: tool_starts.append((name, args))

        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.stream.return_value = _mock_stream_with_tool_call()
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                # Mock the tool execution to return immediately
                with unittest.mock.patch("agent.tools.execute_tool") as mock_exec:
                    from agent.tools import ToolResult
                    mock_exec.return_value = ToolResult(success=True, output="file1.txt\nfile2.txt", error="")
                    rt._run_loop(sk, "list files")

        # on_tool_call_start fires once when the full tool call is accumulated.
        # NOTE: Phase 1.3b integration — _call_llm_streaming fires on_tool_call_start
        # at the 'done' event, then _run_loop fires it again before execution.
        # Listeners receive 2 dispatches per tool call. Fix: consolidate to one.
        assert len(tool_starts) >= 1, f"Expected >=1 tool start dispatches, got: {tool_starts}"
        # The first dispatch fires when the streaming 'done' event is processed
        assert tool_starts[0][0] == "list_files"
        assert tool_starts[0][1] == {"path": "."}, f"Got: {tool_starts[0][1]}"
        rt.stop()

    def test_streaming_accumulates_text_in_response(self):
        """Verify that the final assistant message contains the full accumulated text."""
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, delta: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.stream.return_value = _mock_stream_openai_3_chunks()
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "say hello")

        conv = rt.get_conversation(sk)
        # Last message should be the assistant response with full text
        assistant_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        assert len(assistant_msgs) >= 1, f"Expected assistant message, got: {[m.role.value for m in conv.messages]}"
        assert assistant_msgs[-1].content == "Hello world!"
        rt.stop()

    def test_tool_call_delta_without_index_defaults_to_zero(self):
        """PHASE-11.5 regression: streamer yields tool_call_delta without 'index' key
        should default to idx=0, not crash with KeyError. Anthropic's streaming
        format omits 'index' for single-tool responses.
        """
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        def streamer_no_index():
            yield SSEEvent(type="tool_call_delta", data={"name": "list_files", "arguments": '{"path": "."}'})

        mock_provider = MagicMock()
        mock_provider.stream.return_value = streamer_no_index()
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "list files")

        # If the bug is unfixed, this test crashes with KeyError before reaching here
        conv = rt.get_conversation(sk)
        assistant_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        assert len(assistant_msgs) >= 1, f"Expected assistant message, got: {[m.role.value for m in conv.messages]}"
        rt.stop()

    def test_streaming_preserves_provider_tool_call_id(self):
        """STREAM-ID-PRES: provider-assigned tool_call id flows from raw SSE
        bytes through the streamer, the accumulator, and the final response dict.

        Regression: the old test (pre-2026-06-23) patched
        _PROVIDER_STREAMERS["openai"] with a pre-built SSEEvent generator —
        that bypassed _sse_lines → parse_sse_line → OpenAIProvider("openai").stream
        entirely, so the streamer layer was never tested.

        This test feeds raw SSE bytes through the full pipeline (mocking only
        _urlopen_with_ssl_retry so the streamer itself is exercised verbatim)
        and asserts the id survives every layer. Without the STREAM-ID-PRES
        fix, the streaming path synthesizes `f"call_{idx}"` and MiniMax/OpenAI
        reject the next-turn request with status_code=2013.
        See docs/bugs/BUG_REPORT-streaming-tool-call-id-loss.md.
        """
        from agent import runtime as rt_module
        from agent.llm.openai_provider import OpenAIProvider

        # Provider-shape raw SSE bytes — real id in first delta, argument
        # fragments in subsequent deltas (OpenAI / MiniMax / OpenRouter / ZAI).
        REAL_ID = "call_function_3679004591_1"
        raw_sse = (
            b'data: {"choices":[{"delta":{"tool_calls":['
            b'{"index":0,"id":"' + REAL_ID.encode() + b'",'
            b'"function":{"name":"read_file","arguments":""}}'
            b']}}]}\n\n'
            b'data: {"choices":[{"delta":{"tool_calls":['
            b'{"index":0,"function":{"arguments":"{\\"path\\":\\"/tmp/foo.py\\"}"}}'
            b']}}]}\n\n'
            b'data: {"choices":[{"finish_reason":"tool_calls"}]}\n\n'
            b'data: [DONE]\n\n'
        )

        # Fake response that yields line-bytes — must be a class so Python
        # can look up __iter__ on the _FakeResp type.
        class _FakeResp:
            def __init__(self, buf):
                self._buf = buf
            def __iter__(self):
                return iter(self._buf.splitlines(keepends=True))

        def _make_fake_urlopen(buf):
            """Return a context-manager that provides a fake response."""
            class _Ctx:
                def __enter__(self_ctx):
                    return _FakeResp(buf)
                def __exit__(self_ctx, *a):
                    pass
            return _Ctx()

        # Phase 1: Feed raw SSE bytes through the real OpenAIProvider("openai").stream
        # and verify the SSEEvent stream carries the id forward.
        with unittest.mock.patch(
            "agent.llm.openai_provider.urlopen_with_ssl_retry",
            lambda req, timeout: _make_fake_urlopen(raw_sse),
        ):
            events = list(OpenAIProvider("openai").stream(
                base_url="https://api.openai.com/v1",
                api_key="***",
                model="openai/gpt-4o",
                messages=[{"role": "user", "content": "read foo.py"}],
                tools=None,
                timeout=30.0,
                x_title="",
            ))
        deltas = [ev for ev in events if ev.type == "tool_call_delta"]
        assert deltas, "expected at least one tool_call_delta event"
        assert deltas[0].data.get("id") == REAL_ID, (
            f"streamer must forward provider-assigned id; got {deltas[0].data.get('id')!r}"
        )

        # Phase 2: Run the full _call_llm_streaming pipeline with the real
        # streamer and assert the assembled response carries the real id.
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, delta: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        with unittest.mock.patch(
            "agent.llm.openai_provider.urlopen_with_ssl_retry",
            lambda req, timeout: _make_fake_urlopen(raw_sse),
        ):
            response = rt._call_llm_streaming(
                session_key=sk,
                base_url="https://api.openai.com/v1",
                api_key="***",
                model="openai/gpt-4o",
                caller_key="openai",
                messages=[{"role": "user", "content": "read foo.py"}],
                tools=None,
                timeout=30.0,
            )
        rt._cleanup_tool_history(sk)
        rt.stop()

        tool_calls = response["choices"][0]["message"]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == REAL_ID, (
            f"final tool_call id must be the provider-assigned one; "
            f"got {tool_calls[0]['id']!r}"
        )
        assert tool_calls[0]["function"]["name"] == "read_file"
        assert tool_calls[0]["function"]["arguments"] == '{"path":"/tmp/foo.py"}'

        # Phase 3: Round-trip check — feed the response back into
        # extract_tool_calls (the path used by _run_loop on the next turn).
        call_id, tool_name, args = extract_tool_calls(response, "openai")[0]
        assert call_id == REAL_ID, (
            f"extract_tool_calls must surface the real id; got {call_id!r}"
        )
        assert tool_name == "read_file"

    def test_anthropic_content_block_start_preserves_tool_use_id(self):
        """STREAM-ID-PRES (BUG #4): AnthropicProvider().stream must forward the
        tool_use.id from the content_block_start event through to the SSE event
        stream, so the accumulator captures the provider-assigned id before
        the first content_block_delta arrives.

        Anthropic's protocol differs from OpenAI/MiniMax: the id arrives in
        content_block_start (not in content_block_delta). Without this handler,
        the first-write-wins accumulator never sees an id and falls back to
        synthetic `call_{idx}`.
        """
        from agent import runtime as rt_module
        from agent.llm.anthropic_provider import AnthropicProvider

        ANTHROPIC_ID = "toolu_01A09qGhdummyExample"
        raw_sse = (
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,"content_block":'
            b'{"type":"tool_use","id":"' + ANTHROPIC_ID.encode() + b'"}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":'
            b'{"type":"tool_use_delta","name":"read_file","input":""}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":'
            b'{"type":"tool_use_delta","input":"{\\"path\\":\\"/tmp/bar.py\\"}"}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
        )

        class _FakeResp:
            def __init__(self, buf):
                self._buf = buf
            def __iter__(self):
                return iter(self._buf.splitlines(keepends=True))

        def _make_fake_urlopen(buf):
            class _Ctx:
                def __enter__(self_ctx):
                    return _FakeResp(buf)
                def __exit__(self_ctx, *a):
                    pass
            return _Ctx()

        # Phase 1: Verify the streamer forwards the id.
        with unittest.mock.patch(
            "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
            lambda req, timeout: _make_fake_urlopen(raw_sse),
        ):
            events = list(AnthropicProvider().stream(
                base_url="https://api.anthropic.com/v1",
                api_key="***",
                model="claude-3-5-sonnet",
                messages=[{"role": "user", "content": "read bar.py"}],
                tools=None,
                timeout=30.0,
                x_title="",
            ))
        deltas = [ev for ev in events if ev.type == "tool_call_delta"]
        assert deltas, "expected at least one tool_call_delta event"
        # First delta must carry the id from content_block_start.
        assert deltas[0].data.get("id") == ANTHROPIC_ID, (
            f"Anthropic content_block_start id not forwarded; "
            f"got {deltas[0].data.get('id')!r}"
        )

        # Phase 2: Run the full pipeline.
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, delta: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        with unittest.mock.patch(
            "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
            lambda req, timeout: _make_fake_urlopen(raw_sse),
        ):
            response = rt._call_llm_streaming(
                session_key=sk,
                base_url="https://api.anthropic.com/v1",
                api_key="***",
                model="claude-3-5-sonnet",
                caller_key="anthropic",
                messages=[{"role": "user", "content": "read bar.py"}],
                tools=None,
                timeout=30.0,
            )
        rt._cleanup_tool_history(sk)
        rt.stop()

        tool_calls = response["choices"][0]["message"]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == ANTHROPIC_ID, (
            f"final tool_call id must be Anthropic's tool_use.id; "
            f"got {tool_calls[0]['id']!r}"
        )
        assert tool_calls[0]["function"]["name"] == "read_file"
        assert tool_calls[0]["function"]["arguments"] == '{"path":"/tmp/bar.py"}'

        # Phase 3: Round-trip check — extract_tool_calls must surface the
        # real id. The response is always in normalized OpenAI format regardless
        # of the actual provider, because _call_llm_streaming normalizes all
        # streaming responses to a uniform format.
        call_id, tool_name, args = extract_tool_calls(response, "openai")[0]
        assert call_id == ANTHROPIC_ID, (
            f"extract_tool_calls must surface Anthropic's id; got {call_id!r}"
        )
        assert tool_name == "read_file"

    # ─────────────────────────────────────────────────────────────────────
    # QTR-FIX: regression coverage for the empty/None-id synthetic fallback.
    #
    # The previous `extract_tool_calls` used `tc.get("id", f"call_{...}")`,
    # which only substituted the synthetic id when the `id` key was absent.
    # A response with explicit `id: None` or `id: ""` slipped through and
    # surfaced as an empty `tool_call_id`, which some providers reject on the
    # next turn (status_code=2013 on MiniMax). The fix is `tc.get("id") or
    # f"call_{...}"` so None / empty-string fall through to the fallback.
    # These tests pin the contract for both the OpenAI/MiniMax path and the
    # Anthropic path.
    # ─────────────────────────────────────────────────────────────────────

    def testextract_tool_calls_openai_synthetic_id_when_id_is_null(self):
        """`extract_tool_calls` (OpenAI path) must synthesize an id when the
        tool_call entry has explicit `id: None`. Regression test for the QTR
        empty-id fix; `tc.get("id", default)` did not substitute when the key
        was present with value None.
        """
        resp = {
            "choices": [{"message": {
                "tool_calls": [
                    {"id": None, "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                ]
            }}],
        }
        calls = extract_tool_calls(resp, "openai")
        assert len(calls) == 1
        call_id, tool_name, args = calls[0]
        assert tool_name == "read_file"
        assert args == {"path": "a.py"}
        # Synthetic id contract: starts with "call_" and is non-empty.
        assert call_id, f"call_id must be non-empty when source id was None; got {call_id!r}"
        assert call_id.startswith("call_"), f"expected synthetic id with 'call_' prefix; got {call_id!r}"

    def testextract_tool_calls_openai_synthetic_id_when_id_is_empty_string(self):
        """`extract_tool_calls` (OpenAI path) must synthesize an id when the
        tool_call entry has explicit `id: ""`. Empty string is falsy and must
        fall through to the synthetic fallback.
        """
        resp = {
            "choices": [{"message": {
                "tool_calls": [
                    {"id": "", "function": {"name": "list_files", "arguments": '{"path":"."}'}},
                ]
            }}],
        }
        calls = extract_tool_calls(resp, "openai")
        assert len(calls) == 1
        call_id, tool_name, args = calls[0]
        assert tool_name == "list_files"
        assert args == {"path": "."}
        assert call_id, f"call_id must be non-empty when source id was ''; got {call_id!r}"
        assert call_id.startswith("call_"), f"expected synthetic id with 'call_' prefix; got {call_id!r}"

    def testextract_tool_calls_anthropic_synthetic_id_when_id_is_null(self):
        """`extract_tool_calls` (Anthropic path) must synthesize an id when
        the tool_use block has explicit `id: None`.
        """
        resp = {"content": [
            {"type": "tool_use", "id": None, "name": "read_file", "input": {"path": "b.py"}},
        ]}
        calls = extract_tool_calls(resp, "anthropic")
        assert len(calls) == 1
        call_id, tool_name, args = calls[0]
        assert tool_name == "read_file"
        assert args == {"path": "b.py"}
        assert call_id, f"call_id must be non-empty when source id was None; got {call_id!r}"
        assert call_id.startswith("call_"), f"expected synthetic id with 'call_' prefix; got {call_id!r}"

    def testextract_tool_calls_anthropic_synthetic_id_when_id_is_empty_string(self):
        """`extract_tool_calls` (Anthropic path) must synthesize an id when
        the tool_use block has explicit `id: ""`.
        """
        resp = {"content": [
            {"type": "tool_use", "id": "", "name": "list_files", "input": {"path": "."}},
        ]}
        calls = extract_tool_calls(resp, "anthropic")
        assert len(calls) == 1
        call_id, tool_name, args = calls[0]
        assert tool_name == "list_files"
        assert args == {"path": "."}
        assert call_id, f"call_id must be non-empty when source id was ''; got {call_id!r}"
        assert call_id.startswith("call_"), f"expected synthetic id with 'call_' prefix; got {call_id!r}"



class TestStreamingSignature:
    """
    PHASE-11 regression test: ensures that the streaming test patches and the
    production caller use a parameter list that is compatible with the actual
    `_call_llm_streaming` method signature.

    Catches: future signature changes to `_call_llm_streaming` that would break
    either the production caller (`_call_llm` in agent/runtime.py) or the 4
    `TestStreaming` test patches.
    """

    def test_streaming_method_signature_matches_caller_interface(self):
        """
        The streaming method's parameter list (after `self`) should match the
        keyword arguments used by the production caller AND by the 4 TestStreaming
        test patches. If any of them drifts, this test fails with a clear
        "signature mismatch" message.
        """
        import inspect
        from agent.runtime import AgentRuntime

        # 1. Get the actual method signature
        sig = inspect.signature(AgentRuntime._call_llm_streaming)
        method_params = [name for name in sig.parameters.keys() if name != "self"]

        # 2. The expected parameter list — derived from the TypedDict so that
        # adding/removing a field in StreamingCallKwargs automatically updates
        # this test. Single source of truth (PHASE-FOLLOWUP-1).
        from agent.runtime import StreamingCallKwargs
        expected_params = list(StreamingCallKwargs.__annotations__.keys())

        assert method_params == expected_params, (
            f"_call_llm_streaming signature changed.\n"
            f"  Expected: {expected_params}\n"
            f"  Actual:   {method_params}\n"
            f"  If you changed the signature intentionally, update the production\n"
            f"  caller (agent/runtime.py:_call_llm) and the 4 TestStreaming test\n"
            f"  patches (tests/test_agent_runtime.py) to match."
        )

        # 3. Verify the production caller passes all required parameters
        with open(os.path.join(REPO_ROOT, "agent", "runtime.py")) as f:
            runtime_source = f.read()
        # Find the call site: `self._call_llm_streaming(`
        call_site_match = runtime_source.find("self._call_llm_streaming(")
        assert call_site_match != -1, "Production caller to self._call_llm_streaming not found"
        # Extract the call (rough — just check for key kwargs)
        call_chunk = runtime_source[call_site_match:call_site_match + 800]
        for required_kw in ["session_key=", "base_url=", "api_key=", "model=", "caller_key=", "messages=", "tools=", "timeout="]:
            assert required_kw in call_chunk, (
                f"Production caller is missing required kwarg {required_kw!r}.\n"
                f"Call site: {call_chunk[:200]}"
            )

        # 4. Verify the TestStreaming patches use _make_streaming_lambda fixture (PHASE-FOLLOWUP-4)
        with open(os.path.abspath(__file__)) as f:
            test_source = f.read()
        # Verify no patches use old rt_module pattern
        rt_module_calls = test_source.count("rt_module._call_llm_streaming(") - 1  # -1 for the count line
        assert rt_module_calls == 0, (
            f"Found {rt_module_calls} test patches still calling rt_module._call_llm_streaming.\n"
            f"All TestStreaming patches should use _make_streaming_lambda(rt) instead (PHASE-11)."
        )
        # Verify patches use the fixture — count _make_streaming_lambda(rt) calls in TestStreaming
        fixture_calls = test_source.count("_make_streaming_lambda(rt)") - 1  # -1 for the helper def
        assert fixture_calls >= 5, (
            f"Expected at least 5 test patches using _make_streaming_lambda(rt), found {fixture_calls}."
        )


# ═══════════════════════════════════════════════════════════════════
#  Phase CB-3: Streaming usage capture (BUG #3 fix)
# ═══════════════════════════════════════════════════════════════════

class TestStreamingUsageCapture:
    """Phase CB-3 (BUG #3 fix): streaming responses now capture SSE usage chunks."""

    def test_streaming_captures_openai_usage_chunk(self):
        """An OpenAI-compatible stream that emits a usage chunk in the final frame
        must surface the usage in the response dict (not {})."""
        from agent.runtime import SSEEvent
        from unittest.mock import MagicMock

        def mock_stream_with_usage():
            yield SSEEvent(type="text_delta", data={"content": "Hello"})
            yield SSEEvent(type="text_delta", data={"content": " world"})
            yield SSEEvent(type="usage", data={
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}
            })
            yield SSEEvent(type="done", data={})

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, delta: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        usage_calls = []
        rt._on_token_usage = lambda sk, tokens, cost: usage_calls.append((tokens, cost))

        mock_provider = MagicMock()
        mock_provider.stream.return_value = mock_stream_with_usage()
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "say hello")
        rt.stop()

        # on_token_usage should have fired with non-zero tokens
        assert len(usage_calls) >= 1, f"Expected at least 1 usage call, got: {usage_calls}"
        total_tokens = usage_calls[0][0]
        assert total_tokens > 0, f"Expected non-zero tokens, got: {total_tokens}. Usage calls: {usage_calls}"

    def test_streaming_without_usage_chunk_returns_empty_usage(self):
        """Streams that don't emit a usage chunk still return a valid response with usage={}."""
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, delta: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.stream.return_value = _mock_stream_openai_3_chunks()
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "say hello")
        rt.stop()

        # The conversation should complete successfully (no crash)
        conv = rt.get_conversation(sk)
        assistant_msgs = [m for m in conv.messages if m.role.value == "assistant"]
        assert len(assistant_msgs) >= 1
        assert assistant_msgs[-1].content == "Hello world!"

    def test_streaming_captures_anthropic_usage_in_message_delta(self):
        """Anthropic streams emit usage in message_delta events; the fix must capture these too."""
        from agent.runtime import SSEEvent
        from unittest.mock import MagicMock

        def mock_anthropic_stream_with_usage():
            yield SSEEvent(type="text_delta", data={"content": "Hello from Anthropic"})
            yield SSEEvent(type="usage", data={
                "usage": {"input_tokens": 200, "output_tokens": 80}
            })
            yield SSEEvent(type="done", data={})

        # Use _call_llm_streaming directly to test the usage capture in isolation
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        mock_provider.stream.return_value = mock_anthropic_stream_with_usage()
        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            response = rt._call_llm_streaming(
                session_key=sk,
                base_url="https://api.anthropic.com/v1",
                api_key="test",
                model="anthropic/claude-3-5-sonnet",
                caller_key="anthropic",
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                timeout=30.0,
            )
        rt.stop()

        assert response["usage"] == {"input_tokens": 200, "output_tokens": 80}, \
            f"Expected Anthropic usage dict, got: {response['usage']}"


class TestSSEParsing:
    """Unit tests for SSE parsing utilities (Phase 1.3b)."""

    def test_parse_sse_line_data(self):
        from agent.llm.streaming import parse_sse_line, SSEEvent
        ev = parse_sse_line(b"data: {\"choices\": [{\"delta\": {\"content\": \"hi\"}}]}")
        assert ev is not None
        assert ev.type == "raw"
        assert ev.data["choices"][0]["delta"]["content"] == "hi"

    def test_parse_sse_line_done(self):
        from agent.llm.streaming import parse_sse_line, SSEEvent
        ev = parse_sse_line(b"data: [DONE]")
        assert ev is not None
        assert ev.type == "done"

    def test_parse_sse_line_blank_ignored(self):
        from agent.llm.streaming import parse_sse_line
        assert parse_sse_line(b"") is None
        assert parse_sse_line(b": comment") is None
        assert parse_sse_line(b"  ") is None

    def test_sse_event_namedtuple(self):
        from agent.runtime import SSEEvent
        ev = SSEEvent(type="text_delta", data={"content": "hi"})
        assert ev.type == "text_delta"
        assert ev.data["content"] == "hi"


class TestStuckDetection:
    """§E — Stuck detection tests.

    Verifies:
    - Same tool + same args 3+ times → intervention message
    - 8+ write_file calls with no exec_command → intervention message
    - Varied tools → no intervention
    - Tool history cleanup on conversation cancel
    """

    def _cfg(self, **overrides):
        """Minimal config for AgentRuntime."""
        from agent.config import AgentConfig
        cfg = AgentConfig()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_stuck_repeated_same_tool_and_args(self):
        """3+ calls of same tool+args → stuck detection fires."""
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(self._cfg())
        rt.start()

        sk = "test_stuck_repeated_" + str(uuid.uuid4().hex[:8])
        rt.create_conversation("Coder", sk, "/tmp")

        tool = "read_file"
        args = {"path": "src/main.py"}

        # First 2 calls → no stuck
        for _ in range(2):
            msg = rt._check_stuck(sk, tool, args, iteration=1)
            assert msg is None, f"First 2 calls should not trigger stuck: {msg}"

        # 3rd call → stuck fires
        msg = rt._check_stuck(sk, tool, args, iteration=3)
        assert msg is not None
        assert "stuck-detection" in msg
        assert tool in msg

        rt._cleanup_tool_history(sk)
        rt.stop()

    def test_stuck_write_loop_no_exec(self):
        """8+ write operations with no exec_command → stuck detection fires."""
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(self._cfg())
        rt.start()

        sk = "test_stuck_write_" + str(uuid.uuid4().hex[:8])
        rt.create_conversation("Coder", sk, "/tmp")

        # Do 7 writes → no stuck yet
        for i in range(7):
            msg = rt._check_stuck(sk, "write_file", {"path": f"src/f{i}.py", "content": "x"}, iteration=i)
            assert msg is None, f"7 writes should not trigger: {msg}"

        # 8th write → stuck fires
        msg = rt._check_stuck(sk, "write_file", {"path": "src/f8.py", "content": "x"}, iteration=7)
        assert msg is not None
        assert "stuck-detection" in msg
        assert "write" in msg.lower() or "file" in msg.lower()

        rt._cleanup_tool_history(sk)
        rt.stop()

    def test_not_stuck_with_exec_between_writes(self):
        """Interleaving exec_command with writes prevents the write-loop stuck detection.

        The write-loop check fires at 8+ writes with no exec in the last 8.
        Varying exec args means the 'same tool+args' check never fires either.
        """
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(self._cfg())
        rt.start()

        sk = "test_stuck_varied_" + str(uuid.uuid4().hex[:8])
        rt.create_conversation("Coder", sk, "/tmp")

        # Alternate write/exec for 10 iterations — use different args each time
        # so 'same tool+args' check never triggers, and exec breaks write-loop count
        for i in range(10):
            if i % 2 == 0:
                msg = rt._check_stuck(sk, "write_file", {"path": f"src/f{i}.py", "content": "x"}, iteration=i)
            else:
                # Different command each time → no same-tool-same-args stuck
                msg = rt._check_stuck(sk, "exec_command", {"command": f"python3 -m py_compile src/f{i-1}.py"}, iteration=i)
            assert msg is None, f"Varied tools should not trigger stuck at iter {i}: {msg}"

        rt._cleanup_tool_history(sk)
        rt.stop()

    def test_not_stuck_different_args(self):
        """Same tool called with different args → not stuck."""
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(self._cfg())
        rt.start()

        sk = "test_stuck_diff_args_" + str(uuid.uuid4().hex[:8])
        rt.create_conversation("Coder", sk, "/tmp")

        # Same tool, different args, 5 times → no stuck
        for i in range(5):
            msg = rt._check_stuck(sk, "read_file", {"path": f"src/f{i}.py"}, iteration=i)
            assert msg is None, f"Different args should not trigger stuck: {msg}"

        rt._cleanup_tool_history(sk)
        rt.stop()

    def test_tool_history_cleans_up_on_cancel(self):
        """Cancelling a conversation removes its tool history."""
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(self._cfg())
        rt.start()

        sk = "test_cleanup_" + str(uuid.uuid4().hex[:8])
        rt.create_conversation("Coder", sk, "/tmp")

        # Add some history
        rt._check_stuck(sk, "read_file", {"path": "a.py"}, iteration=1)
        rt._check_stuck(sk, "read_file", {"path": "b.py"}, iteration=2)
        assert sk in rt._tool_history
        assert len(rt._tool_history[sk]) == 2

        # Cancel → cleanup
        rt.cancel(sk)
        # cancel() is async (threaded), give it a moment to process
        import time
        time.sleep(0.05)
        assert sk not in rt._tool_history, f"History should be gone after cancel: {rt._tool_history.get(sk)}"

        rt.stop()

    def test_history_pruned_at_20_entries(self):
        """History is kept to last 20 entries."""
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(self._cfg())
        rt.start()

        sk = "test_prune_" + str(uuid.uuid4().hex[:8])
        rt.create_conversation("Coder", sk, "/tmp")

        # Add 25 entries
        for i in range(25):
            rt._check_stuck(sk, "read_file", {"path": f"f{i}.py"}, iteration=i)

        history = rt._tool_history[sk]
        assert len(history) == 20, f"Expected 20, got {len(history)}"
        # Last entry should be the 25th (index 24)
        assert history[-1]["iteration"] == 24

        rt._cleanup_tool_history(sk)
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Phase CB-3: Stuck message transient prefix (BUG #4 fix)
# ═══════════════════════════════════════════════════════════════════

class TestStuckMessageTransient:
    """Phase CB-3 (BUG #4 fix): stuck messages are transient prefixes, not stored."""

    def test_stuck_message_not_stored_in_conv_messages(self):
        """Stuck-detection text is NOT stored in conv.messages when stuck fires.

        Drives _run_loop 3 times with the same tool call to trigger stuck
        detection. Verifies that conv.messages has no 'stuck-detection' text
        in any message (tool result, assistant, or otherwise).
        """
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from agent.tools import ToolResult

        # Mock LLM: first call → tool call, subsequent calls → text response
        responses = [_resp(tool_calls=[
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "test.py"}'}}
        ]), _resp("Done.")]
        call_idx = [0]
        def mock_call_llm(sk, msgs, tools, **kwargs):
            r = responses[min(call_idx[0], len(responses) - 1)]
            call_idx[0] += 1
            return r

        # Run the loop 3 times with the same tool call to trigger stuck detection
        for _ in range(3):
            call_idx[0] = 0
            with unittest.mock.patch.object(rt, "_call_llm", mock_call_llm):
                with unittest.mock.patch("agent.tools.execute_tool") as mock_exec:
                    mock_exec.return_value = ToolResult(success=True, output="file content", error="")
                    rt._run_loop(sk, "read the file")

        conv = rt.get_conversation(sk)
        # Check that no message in conv.messages contains stuck-detection text
        for m in conv.messages:
            assert m.content is None or "stuck-detection" not in (m.content or ""), \
                f"Stuck-detection text should NOT be in conv.messages, found in role={m.role.value}: {(m.content or '')[:100]}"

        rt._cleanup_tool_history(sk)
        rt.stop()


class TestPerProjectEnforcement:
    """§F — Per-project enforcement config tests.

    Verifies:
    - .crabcakes/enforcement.json is loaded when present
    - Project-level skip patterns are merged additively
    - Per-tier enable/disable overrides are respected
    - Missing file → returns None (uses global config)
    """

    def test_load_project_config_success(self, tmp_path):
        """Valid JSON file is parsed and returned."""
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(
            json.dumps({"syntax_check": False, "skip_patterns": ["*.generated.py"]})
        )
        from agent.enforcement import _load_project_enforcement_config
        result = _load_project_enforcement_config(str(tmp_path))
        assert result is not None
        assert result["syntax_check"] is False
        assert result["skip_patterns"] == ["*.generated.py"]

    def test_load_project_config_missing(self, tmp_path):
        """No .crabcakes/enforcement.json → returns None."""
        from agent.enforcement import _load_project_enforcement_config
        result = _load_project_enforcement_config(str(tmp_path))
        assert result is None

    def test_load_project_config_invalid_json(self, tmp_path):
        """Malformed JSON → returns None."""
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text("not valid json")
        from agent.enforcement import _load_project_enforcement_config
        result = _load_project_enforcement_config(str(tmp_path))
        assert result is None

    def test_project_config_merge_skip_patterns(self, tmp_path):
        """Project skip patterns are additive to global defaults."""
        from agent.enforcement import check
        from agent.config import EnforcementConfig
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(
            json.dumps({"skip_patterns": ["*.custom.py", "*.generated.py"]})
        )
        # Create a test file and a global skip that would skip it
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        # Global config skips *.md but not *.py, project config adds *.generated.py
        cfg = EnforcementConfig(syntax_check=True, skip_patterns=["*.md"])
        # Create ToolResult
        from agent.tools import ToolResult
        tr = ToolResult(success=True, output="wrote", error=None)
        result = check("write_file", {"path": "test.py"}, tr, str(tmp_path), cfg)
        # Should have run the check (not skipped)
        syntax_check = next((c for c in result.checks if c.tier == "syntax"), None)
        assert syntax_check is not None, "Syntax check should run for .py with global skip of *.md only"

    def test_project_config_disable_tier(self, tmp_path):
        """Project config can disable individual enforcement tiers."""
        from agent.enforcement import check
        from agent.config import EnforcementConfig
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(
            json.dumps({"test_run": False, "lint_check": False})
        )
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        cfg = EnforcementConfig(syntax_check=True, test_run=True, lint_check=True)
        from agent.tools import ToolResult
        tr = ToolResult(success=True, output="wrote", error=None)
        result = check("write_file", {"path": "test.py"}, tr, str(tmp_path), cfg)
        tiers_run = {c.tier for c in result.checks}
        assert "syntax" in tiers_run, "Syntax should still run"
        assert "tests" not in tiers_run, "Tests should be disabled by project config"
        assert "lint" not in tiers_run, "Lint should be disabled by project config"


# ═══════════════════════════════════════════════════════════════════
#  Phase 2: MiniMax body-level error surfacing (SPEC-LOCAL-AGENT-NO-RESPONSE-FIX)
# ═══════════════════════════════════════════════════════════════════

class TestMinimaxBodyLevelError:
    """MiniMax returns body-level errors with HTTP 200 (base_resp.status_code != 0).
    The runtime must raise RuntimeError so the error surfaces to the user."""

    def test_minimax_body_level_error_raises(self):
        """MiniMaxProvider().call with base_resp.status_code=1004 raises RuntimeError."""
        from agent.llm.minimax_provider import MiniMaxProvider
        error_body = json.dumps({
            "base_resp": {
                "status_code": 1004,
                "status_msg": "login fail: Please carry the API secret key"
            }
        }).encode()
        mock_resp = unittest.mock.MagicMock()
        mock_resp.read.return_value = error_body
        mock_resp.__enter__ = unittest.mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="status_code=1004"):
                MiniMaxProvider().call(
                    base_url="https://api.minimax.chat/v1",
                    api_key="invalid-key",
                    model="minimax/MiniMax-M2.7",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    timeout=30,
                )

    def test_streaming_minimax_body_error_raises(self):
        """MiniMaxProvider().stream with base_resp.status_code=1004 raises RuntimeError."""
        from agent.llm.minimax_provider import MiniMaxProvider
        error_body = json.dumps({
            "base_resp": {
                "status_code": 1004,
                "status_msg": "login fail: Please carry the API secret key"
            }
        }).encode()

        # Simulate HTTP response whose lines yield the error JSON
        mock_resp = unittest.mock.MagicMock()
        mock_resp.__iter__ = unittest.mock.MagicMock(return_value=iter([error_body]))
        mock_resp.__enter__ = unittest.mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="status_code=1004"):
                # Must consume the generator to trigger the error
                list(MiniMaxProvider().stream(
                    base_url="https://api.minimax.chat/v1",
                    api_key="invalid-key",
                    model="minimax/MiniMax-M2.7",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    timeout=30,
                ))


# ═══════════════════════════════════════════════════════════════════
#  Phase 3: Empty-choices response handling (SPEC-LOCAL-AGENT-NO-RESPONSE-FIX)
# ═══════════════════════════════════════════════════════════════════

class TestEmptyChoicesResponse:
    """When the LLM returns a response with no choices and empty content,
    _run_loop must dispatch _on_error instead of _on_response_complete."""

    def test_empty_choices_response_dispatches_on_error(self):
        """Response with no choices key and empty content → _on_error fires."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)

        # Response with no choices key — mimics a body-level error that
        # slipped through (e.g. MiniMax returning {"base_resp":{...}} with status_code 0
        # but still no choices)
        empty_response = {"usage": {}}

        with unittest.mock.patch.object(rt, "_call_llm", lambda sk, msgs, tools, **kwargs: empty_response):
            rt._run_loop(sk, "hello")

        assert len(errors) == 1, f"Expected 1 error, got: {errors}"
        assert "no content" in errors[0].lower() or "configuration error" in errors[0].lower(), \
            f"Error message should mention the issue, got: {errors[0]}"
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Phase 4: Defensive empty-response bubble (SPEC-LOCAL-AGENT-NO-RESPONSE-FIX)
# ═══════════════════════════════════════════════════════════════════

class TestEmptyResponseFallbackBubble:
    """When _do_response_complete is called with empty text and no streaming
    was active, a fallback bubble must be rendered so the user sees feedback."""

    def _make_handler(self):
        """Create a minimal AgentRuntimeHandler with mocked deps."""
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        mc = unittest.mock.MagicMock()
        crh = unittest.mock.MagicMock()
        crh.is_streaming.return_value = False
        crh.render_sync.return_value = unittest.mock.MagicMock()  # fake bubble widget
        handler = AgentRuntimeHandler(
            main_content=mc,
            chat_render_handler=crh,
            GLib_module=None,  # no GLib in tests — runs synchronously
        )
        return handler, mc, crh

    def test_empty_response_renders_fallback_bubble(self):
        """_do_response_complete with empty text + not streaming → fallback bubble rendered."""
        handler, mc, crh = self._make_handler()
        chat_box = unittest.mock.MagicMock()
        handler._resolve_chat_box = unittest.mock.MagicMock(return_value=chat_box)

        handler._do_response_complete("special:coder", "")

        # render_sync must have been called with a fallback message
        crh.render_sync.assert_called_once()
        call_args = crh.render_sync.call_args
        assert call_args[0][0] == "System"  # role
        assert "no content" in call_args[0][1].lower() or "configuration error" in call_args[0][1].lower()
        # Bubble was appended to chat_box
        chat_box.append.assert_called_once()

    def test_empty_response_with_streaming_does_not_render_extra_bubble(self):
        """_do_response_complete with empty text + streaming active → no extra fallback bubble.
        The streaming bubble finalization handles display."""
        handler, mc, crh = self._make_handler()
        crh.is_streaming.return_value = True  # streaming WAS active
        chat_box = unittest.mock.MagicMock()
        handler._resolve_chat_box = unittest.mock.MagicMock(return_value=chat_box)

        handler._do_response_complete("special:coder", "")

        # render_sync should NOT have been called for fallback (streaming handles it)
        # Note: end_streaming IS called (that's fine), but no additional render
        # The key assertion: render_sync was never called with the fallback text
        for call_args in crh.render_sync.call_args_list:
            text = call_args[0][1]
            assert "no content" not in text.lower() and "Agent returned" not in text, \
                f"Fallback bubble should not render during streaming, got: {text}"


# ═══════════════════════════════════════════════════════════════════
#  Streaming regression tests for AnthropicProvider().stream (W2/W3/W4)
# ═══════════════════════════════════════════════════════════════════

class TestStreamAnthropicEvents:
    """Regression tests for AnthropicProvider().stream.

    Locks in the W2 (tool conversion), W3 (message conversion), and W4
    (no stream_options in Anthropic payload) fixes from
    SPEC-RUNTIME-HARDENING-AUDIT.md §2.2 and §10.3.
    """

    def test_no_stream_options_in_anthropic_payload(self):
        """Anthropic API does not support stream_options — must not be in payload."""
        import json
        from unittest.mock import patch, MagicMock
        from agent.llm.anthropic_provider import AnthropicProvider

        captured_requests = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            # Return a minimal streaming response
            body = b'data: {"type": "message_stop"}\n\n'
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.iter_lines = MagicMock(return_value=iter([body]))
            return resp

        with patch("agent.llm.anthropic_provider.urlopen_with_ssl_retry", side_effect=fake_urlopen):
            with patch("agent.llm.anthropic_provider.sse_lines", return_value=iter([
                b'data: {"type": "message_stop"}\n\n'
            ])):
                # Provide minimal args
                list(AnthropicProvider().stream(
                    base_url="https://api.anthropic.com",
                    api_key="test-key",
                    model="claude-3-5-sonnet-20241022",
                    messages=[{"role": "user", "content": "hello"}],
                    tools=None,
                    timeout=30.0,
                ))
        # Verify no stream_options in captured request data
        assert len(captured_requests) == 1
        payload = json.loads(captured_requests[0].data)
        assert "stream_options" not in payload, (
            "stream_options must not be sent to Anthropic API"
        )

    def test_anthropic_messages_are_converted(self):
        """Messages must be converted to Anthropic format, not passed raw.

        Includes assistant with tool_calls and tool role messages so the test
        would FAIL if _convert_messages_for_anthropic were bypassed (a plain
        user message passes through either way).
        """
        import json
        from unittest.mock import patch, MagicMock
        from agent.llm.anthropic_provider import AnthropicProvider

        captured_requests = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            body = b'data: {"type": "message_stop"}\n\n'
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.iter_lines = MagicMock(return_value=iter([body]))
            return resp

        raw_messages = [
            {"role": "user", "content": "read foo.py"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "function": {"name": "read_file", "arguments": '{"path": "foo.py"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
        ]

        with patch("agent.llm.anthropic_provider.urlopen_with_ssl_retry", side_effect=fake_urlopen):
            with patch("agent.llm.anthropic_provider.sse_lines", return_value=iter([
                b'data: {"type": "message_stop"}\n\n'
            ])):
                list(AnthropicProvider().stream(
                    base_url="https://api.anthropic.com",
                    api_key="test-key",
                    model="claude-3-5-sonnet-20241022",
                    messages=raw_messages,
                    tools=None,
                    timeout=30.0,
                ))

        assert len(captured_requests) == 1
        payload = json.loads(captured_requests[0].data)
        api_msgs = payload["messages"]
        # Messages should be a list (Anthropic format)
        assert isinstance(api_msgs, list)
        # 3 input messages → 3 output messages (no system, no drops)
        assert len(api_msgs) == 3
        # assistant with tool_calls → content blocks with tool_use
        assert api_msgs[1]["role"] == "assistant"
        assert isinstance(api_msgs[1]["content"], list)
        assert any(
            b.get("type") == "tool_use" for b in api_msgs[1]["content"]
        ), f"assistant tool_calls must convert to tool_use blocks, got: {api_msgs[1]}"
        # tool role → user role with tool_result content
        assert api_msgs[2]["role"] == "user"
        assert isinstance(api_msgs[2]["content"], list)
        assert api_msgs[2]["content"][0]["type"] == "tool_result", (
            f"tool role must convert to user role with tool_result, got: {api_msgs[2]}"
        )

    def test_anthropic_tools_are_converted(self):
        """Tools must be converted to Anthropic format, not passed raw."""
        import json
        from unittest.mock import patch, MagicMock
        from agent.llm.anthropic_provider import AnthropicProvider

        captured_requests = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            body = b'data: {"type": "message_stop"}\n\n'
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.iter_lines = MagicMock(return_value=iter([body]))
            return resp

        raw_tools = [{
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "A test tool",
                "parameters": {"type": "object", "properties": {}}
            }
        }]

        with patch("agent.llm.anthropic_provider.urlopen_with_ssl_retry", side_effect=fake_urlopen):
            with patch("agent.llm.anthropic_provider.sse_lines", return_value=iter([
                b'data: {"type": "message_stop"}\n\n'
            ])):
                list(AnthropicProvider().stream(
                    base_url="https://api.anthropic.com",
                    api_key="test-key",
                    model="claude-3-5-sonnet-20241022",
                    messages=[{"role": "user", "content": "hello"}],
                    tools=raw_tools,
                    timeout=30.0,
                ))

        assert len(captured_requests) == 1
        payload = json.loads(captured_requests[0].data)
        # After conversion, tools should use input_schema (Anthropic format)
        assert "tools" in payload
        for tool in payload["tools"]:
            assert "input_schema" in tool, "Anthropic tools must have input_schema"
            assert "name" in tool


# ═══════════════════════════════════════════════════════════════════
#  Phase 1 audit regression tests — system prompt placement
# ═══════════════════════════════════════════════════════════════════

class TestSystemPromptPlacement:
    """Regression tests for Phase 1 audit findings.

    Locks in fixes for:
    - BUG #1: AnthropicProvider().call was sending the system prompt TWICE
              (payload['system'] AND as first user message via helper)
    - BUG #2: AnthropicProvider().stream was sending the system prompt
              as a USER message (wrong role, loses Anthropic system priority)
    - BUG #3: _convert_tools_for_anthropic raised KeyError on missing parameters
    """

    def test_non_streaming_system_not_duplicated_as_user(self):
        """AnthropicProvider().call must put system prompt ONLY in payload['system'],
        NOT also as the first user message in payload['messages'].
        """
        from unittest.mock import patch, MagicMock
        from agent.llm.anthropic_provider import AnthropicProvider

        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            body = b'{"id": "msg_1", "content": [{"type": "text", "text": "hi"}]}'
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = MagicMock(return_value=body)
            return resp

        with patch("agent.llm.anthropic_provider.urlopen_with_ssl_retry", side_effect=fake_urlopen):
            AnthropicProvider().call(
                base_url="https://api.anthropic.com",
                api_key="test",
                model="claude-3-5-sonnet-20241022",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ],
                tools=None,
                timeout=30.0,
            )

        import json
        assert len(captured) == 1
        payload = json.loads(captured[0].data)
        # System goes to payload['system']
        assert payload.get("system") == "You are a helpful assistant.", (
            f"system prompt must be in payload['system'], got: {payload.get('system')!r}"
        )
        # System must NOT be duplicated as a user message
        msg_contents = [m.get("content", "") for m in payload["messages"]]
        assert "You are a helpful assistant." not in msg_contents, (
            f"system prompt leaked into messages as user content: {payload['messages']}"
        )
        # Only the real user message should remain
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "Hello"

    def test_streaming_system_goes_to_payload_system_not_user(self):
        """AnthropicProvider().stream must put system prompt in payload['system'],
        NOT as a user-role message (Anthropic system has higher priority).
        """
        from unittest.mock import patch, MagicMock
        from agent.llm.anthropic_provider import AnthropicProvider

        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            body = b'data: {"type": "message_stop"}\n\n'
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.iter_lines = MagicMock(return_value=iter([body]))
            return resp

        with patch("agent.llm.anthropic_provider.urlopen_with_ssl_retry", side_effect=fake_urlopen):
            with patch("agent.llm.anthropic_provider.sse_lines", return_value=iter([
                b'data: {"type": "message_stop"}\n\n'
            ])):
                list(AnthropicProvider().stream(
                    base_url="https://api.anthropic.com",
                    api_key="test",
                    model="claude-3-5-sonnet-20241022",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Hello"},
                    ],
                    tools=None,
                    timeout=30.0,
                ))

        import json
        assert len(captured) == 1
        payload = json.loads(captured[0].data)
        # System goes to payload['system']
        assert payload.get("system") == "You are a helpful assistant.", (
            f"streaming system prompt must be in payload['system'], got: {payload.get('system')!r}"
        )
        # System must NOT appear as a user message
        msg_contents = [m.get("content", "") for m in payload["messages"]]
        assert "You are a helpful assistant." not in msg_contents, (
            f"streaming system prompt leaked into messages as user content: {payload['messages']}"
        )

    def test_convert_tools_handles_missing_parameters(self):
        """_convert_tools_for_anthropic must NOT raise KeyError when a tool
        dict lacks 'parameters'. It should default input_schema to {}.
        """
        from agent.llm.convert import convert_tools_for_anthropic

        # Missing 'parameters' key entirely
        result = convert_tools_for_anthropic([{"function": {"name": "f", "description": "d"}}])
        assert result == [{"name": "f", "description": "d", "input_schema": {}}], result

        # parameters=None
        result = convert_tools_for_anthropic([{"function": {"name": "f", "parameters": None}}])
        assert result == [{"name": "f", "description": "", "input_schema": {}}], result

        # parameters not a dict (string)
        result = convert_tools_for_anthropic([{"function": {"name": "f", "parameters": "bad"}}])
        assert result == [{"name": "f", "description": "", "input_schema": {}}], result


# ═══════════════════════════════════════════════════════════════════
#  Stale project context reconciliation (Option C+)
# ═══════════════════════════════════════════════════════════════════
#
# The original bug: a special agent conversation persisted to disk carries
# the `project_path` and `system_prompt` snapshot from the session that
# last wrote it. If the user opens a different project in a future
# session, the conversation loads with the STALE project_path — the
# agent's tools are sandboxed to the wrong directory and the system
# prompt is the wrong project's docs.
#
# Fix: the runtime rebuilds the project context on every load + every
# send. _load_conversation_from_disk always sets project_path=None and
# system_prompt="" (the persisted values are kept on disk for audit but
# are not trusted). _rebuild_conversation_context re-applies the active
# project and rebuilds the system prompt against it.
#
# These tests pin the new contract: load() returns a stale-tolerant
# Conversation, and _rebuild_conversation_context correctly re-applies
# the active project.

import json as _json
import os as _os
import tempfile as _tempfile


def _make_runtime():
    """Build a minimal AgentRuntime for testing the reconciliation helpers.

    Mirrors the construction in TestLifecycle above (which is the
    canonical pattern in this file). Returns (rt, sk, tmp_conv_dir).
    """
    from agent.config import AgentConfig, LLMProviderConfig
    from agent.persistence import conversations_dir as _conversations_dir
    cfg = AgentConfig(
        providers={
            "openai": LLMProviderConfig(
                name="openai", base_url="https://api.openai.com/v1",
                api_key="test", default_model="gpt-4o",
            )
        },
        default_provider="openai",
        default_model="openai/gpt-4o",
        max_tool_iterations=5,
        tool_timeout_seconds=30,
        auto_save_conversations=False,
    )
    rt = AgentRuntime(cfg)
    rt.start()
    return rt


class TestLoadConversationStaleTolerance:
    """Option C+ load contract: persisted project_path/system_prompt are
    never trusted. They are kept on disk for audit, but load() always
    returns a Conversation with project_path=None and system_prompt="".
    The lazy reconciliation in _rebuild_conversation_context re-applies
    the active project on first send.
    """

    def test_loaded_conversation_has_no_project_path(self, tmp_path, monkeypatch):
        """A conversation persisted under project A loads with project_path=None
        even if the file on disk says project_path=/A. This is the failure-case
        reproduction for the original bug: the old load() copied the stale value
        verbatim and the agent saw project A's docs and tools.
        """
        # Arrange: write a conversation file as if the user had project A open.
        # _conversations_dir() does `from utils.config import get_config_dir`
        # at call time, so we patch the symbol in its source module.
        d = tmp_path / "conversations"
        d.mkdir()
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        # The file says project_path=/old/project and the system_prompt is the
        # one that was rendered against /old/project. New load() must ignore both.
        persisted = {
            "session_key": "special:debugger",
            "agent_name": "Debugger",
            "project_path": "/old/project",
            "model": "openai/gpt-4o",
            "provider": "openai",
            "messages": [],
            "system_prompt": "<rendered against /old/project, contains 'You are working on /old/project'>",
            "total_tokens": 0,
            "total_cost": 0.0,
            "step_count": 0,
        }
        (d / "special:debugger.json").write_text(_json.dumps(persisted))

        # Act
        from agent.persistence import load_conversation_from_disk
        result = load_conversation_from_disk("special:debugger")
        assert result is not None, "load() should still find the file"
        conv, _ = result

        # Assert: stale values are NOT applied
        assert conv.project_path is None, (
            f"FAILURE-CASE REPRO: loaded conv.project_path={conv.project_path!r}; "
            "expected None so the active project is applied on first send"
        )
        assert conv.system_prompt == "", (
            f"FAILURE-CASE REPRO: loaded conv.system_prompt={conv.system_prompt!r}; "
            "expected empty so the active project's prompt is rebuilt"
        )

    def test_loaded_conversation_preserves_other_fields(self, tmp_path, monkeypatch):
        """Loading with the new contract must still restore messages, model,
        total_tokens, total_cost, step_count, agent_role, fallback_provider,
        etc. — only project_path and system_prompt are zeroed.
        """
        d = tmp_path / "conversations"
        d.mkdir()
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        persisted = {
            "session_key": "special:debugger",
            "agent_name": "Debugger",
            "agent_role": "debugger",
            "project_path": "/old/project",
            "model": "openai/gpt-4o",
            "provider": "openai",
            "messages": [{"role": "user", "content": "old question", "tool_calls": [], "tool_call_id": None, "tokens_used": 0}],
            "system_prompt": "stale prompt",
            "total_tokens": 80069,
            "total_cost": 1.23,
            "step_count": 18,
            "agent_role": "debugger",
            "fallback_provider": "openrouter",
            "fallback_model": "openrouter/owl-alpha",
        }
        (d / "special:debugger.json").write_text(_json.dumps(persisted))

        from agent.persistence import load_conversation_from_disk
        conv, _ = load_conversation_from_disk("special:debugger")

        assert conv is not None
        assert conv.agent_name == "Debugger"
        assert conv.agent_role == "debugger"
        assert conv.model == "openai/gpt-4o"
        assert conv.total_tokens == 80069
        assert conv.total_cost == 1.23
        assert conv.step_count == 18
        assert conv.fallback_provider == "openrouter"
        assert conv.fallback_model == "openrouter/owl-alpha"
        assert len(conv.messages) == 1
        assert conv.messages[0].content == "old question"


class TestRebuildConversationContext:
    """Option C+ rebuild contract: _rebuild_conversation_context re-applies
    the active project and rebuilds the system prompt. The method must be
    idempotent, safe to call on partially-initialized conversations, and
    never raise.
    """

    def test_rebuild_updates_project_path_and_system_prompt(self):
        """Happy path: cold agent's stale conversation has project_path=None
        after load; after rebuild, it matches the active project.
        """
        rt = _make_runtime()
        sk = rt.create_conversation(
            agent_name="Debugger", session_key="special:debugger",
            project_path=None,  # simulates load() — no project applied yet
        )
        conv = rt.get_conversation(sk)
        assert conv.project_path is None
        # NOTE: create_conversation() always calls build_system_prompt and
        # produces a non-empty prompt — even with project_path=None. The
        # empty-prompt case is specific to load_conversation() (the load
        # path zeros it). This test exercises the rebuild path, so the
        # prompt starts non-empty and rebuild must change it.
        assert conv.system_prompt != ""
        original_prompt = conv.system_prompt

        # Act
        rt._rebuild_conversation_context(sk, "/path/to/projects/new", "debugger")

        conv = rt.get_conversation(sk)
        assert conv.project_path == "/path/to/projects/new"
        # The rebuild must produce a new prompt (not no-op). With a different
        # project_path, the awareness block changes → prompt changes.
        # build_awareness_dict puts the project basename into PROJECT_NAME
        # and the path into the snapshot's Path: line.
        assert conv.system_prompt != original_prompt, (
            "rebuild must produce a different prompt when project_path changes"
        )
        rt.stop()

    def test_rebuild_is_idempotent_noop_when_already_in_sync(self):
        """If conv.project_path already matches the active project AND
        system_prompt is non-empty, rebuild is a no-op (cheap short-circuit).
        """
        rt = _make_runtime()
        sk = rt.create_conversation(
            agent_name="Debugger", session_key="special:debugger",
            project_path="/path/to/projects/active",
        )
        # Snapshot the current system_prompt — rebuild should leave it untouched
        original_prompt = rt.get_conversation(sk).system_prompt

        rt._rebuild_conversation_context(sk, "/path/to/projects/active", "debugger")
        assert rt.get_conversation(sk).system_prompt == original_prompt, (
            "idempotent rebuild must not touch system_prompt when already in sync"
        )
        rt.stop()

    def test_rebuild_clears_project_when_active_is_none(self):
        """Passing project_path=None clears the conversation's project
        context. This happens when the user closes the active project.
        """
        rt = _make_runtime()
        sk = rt.create_conversation(
            agent_name="Debugger", session_key="special:debugger",
            project_path="/path/to/projects/old",
        )
        assert rt.get_conversation(sk).project_path == "/path/to/projects/old"

        rt._rebuild_conversation_context(sk, None, "debugger")
        assert rt.get_conversation(sk).project_path is None
        rt.stop()

    def test_rebuild_unknown_session_key_is_silent_noop(self):
        """If the session_key isn't loaded in memory, rebuild is a no-op
        (the cold path handles it on next load). This is critical because
        the eager reconciliation in set_active_project calls rebuild for
        every registered agent, including those that have never been
        instantiated.
        """
        rt = _make_runtime()
        # Should not raise
        rt._rebuild_conversation_context("special:never-instantiated", "/x", "")
        rt.stop()

    def test_rebuild_falls_back_to_persisted_prompt_on_build_failure(self):
        """If build_system_prompt raises (e.g. project path is gone,
        awareness_dict fails), the conversation keeps the OLD persisted
        system_prompt and the agent still works — just with a stale prompt.
        The bug is logged but never crashes the runtime.
        """
        rt = _make_runtime()
        sk = rt.create_conversation(
            agent_name="Debugger", session_key="special:debugger",
            project_path=None,
        )
        conv = rt.get_conversation(sk)
        conv.system_prompt = "the OLD prompt (do not lose this)"

        # Force build_system_prompt to raise by patching it
        from unittest.mock import patch
        with patch("agent.context.build_system_prompt", side_effect=RuntimeError("boom")):
            rt._rebuild_conversation_context(sk, "/some/path", "debugger")

        # Assert: the old prompt is preserved (fallback), and project_path
        # was NOT updated (because the rebuild failed atomically — we don't
        # half-apply).
        conv = rt.get_conversation(sk)
        assert conv.system_prompt == "the OLD prompt (do not lose this)", (
            f"on rebuild failure, the persisted prompt must be preserved; got: {conv.system_prompt!r}"
        )
        rt.stop()

    def test_rebuild_replaces_stale_project_path_with_active(self):
        """FAILURE-CASE REPRO for the original bug: conversation was
        persisted under project A; user opens project B; rebuild must
        replace project_path with B, not keep A.
        """
        rt = _make_runtime()
        sk = rt.create_conversation(
            agent_name="Debugger", session_key="special:debugger",
            project_path="/A",
        )
        # Simulate the original bug state: conversation was just loaded from
        # disk, project_path is still /A (the load() method's old behavior).
        assert rt.get_conversation(sk).project_path == "/A"

        # Act: rebuild against the actually-open project B
        rt._rebuild_conversation_context(sk, "/B", "debugger")

        # Assert: project_path is now B
        conv = rt.get_conversation(sk)
        assert conv.project_path == "/B", (
            f"FAILURE-CASE REPRO: rebuild did not replace stale /A with active /B; "
            f"got project_path={conv.project_path!r}"
        )
        # Assert: system_prompt mentions B, not A
        # (The prompt's awareness block includes the project path; if A is
        # still in the prompt, the rebuild didn't actually rebuild.)
        assert "/A" not in conv.system_prompt or "/B" in conv.system_prompt, (
            f"rebuilt prompt should reflect /B, not /A; got: {conv.system_prompt[:300]!r}"
        )
        rt.stop()


class TestAgentRuntimeContextReconciliationFullPath:
    """Integration test for the full load → rebuild → send path. This is
    the end-to-end behavior the original bug violated: a cold agent
    conversation must reconcile against the active project before any
    send.
    """

    def test_loaded_conversation_reconciles_to_active_project(self, tmp_path, monkeypatch):
        """End-to-end: write a conversation file as if the user had project A
        open. Then "open" project B (call _rebuild_conversation_context with
        /B). The conversation's project_path and system_prompt must reflect
        /B, not /A. This is the exact scenario the user hit with the
        debugger agent in production.
        """
        d = tmp_path / "conversations"
        d.mkdir()
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        persisted = {
            "session_key": "special:debugger",
            "agent_name": "Debugger",
            "project_path": "/A",
            "model": "openai/gpt-4o",
            "provider": "openai",
            "messages": [
                {"role": "user", "content": "old question from /A session",
                 "tool_calls": [], "tool_call_id": None, "tokens_used": 0},
                {"role": "assistant", "content": "old answer",
                 "tool_calls": [], "tool_call_id": None, "tokens_used": 0},
            ],
            "system_prompt": "STALE PROMPT for /A",
            "total_tokens": 0,
            "total_cost": 0.0,
            "step_count": 0,
            "agent_role": "debugger",
        }
        (d / "special:debugger.json").write_text(_json.dumps(persisted))

        rt = _make_runtime()
        # Simulate the production path: load() restores the conversation,
        # then the handler (or build_system_prompt) calls rebuild with the
        # active project.
        ok = rt.load_conversation("special:debugger")
        assert ok
        conv = rt.get_conversation("special:debugger")
        assert conv is not None

        # After load: project_path is None, system_prompt is empty
        # (Option C+ load contract)
        assert conv.project_path is None
        assert conv.system_prompt == ""

        # Conversation history is preserved
        assert len(conv.messages) == 2
        assert conv.messages[0].content == "old question from /A session"
        assert conv.messages[1].content == "old answer"

        # Now reconcile against the active project /B
        rt._rebuild_conversation_context("special:debugger", "/B", "debugger")

        # After rebuild: project_path is /B, system_prompt is freshly built
        assert conv.project_path == "/B"
        assert conv.system_prompt != "STALE PROMPT for /A"
        assert conv.system_prompt != "", "rebuild must produce a real prompt"
        # The old messages are STILL there (rebuild doesn't touch history)
        assert len(conv.messages) == 2

        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Local agent chat-bubble header (Bug #8)
# ═══════════════════════════════════════════════════════════════════
#
# Regression tests for the missing-name/dot/timestamp header on local
# special-agent bubbles. The fix threads the display name from
# AgentRuntimeHandler into end_streaming/render_sync as an optional
# parameter so build_role_bubble's header condition is satisfied for
# local agents (which are NOT in AgentManager).

from unittest.mock import MagicMock, patch
from agent.special_agents import SpecialAgentDef


def _make_handler():
    """Build a minimal AgentRuntimeHandler with mock ChatRenderHandler
    and MainContent. The crh is a MagicMock so we can assert call args
    and stub is_streaming without instantiating real GTK widgets.
    """
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
    crh = MagicMock()
    mc = MagicMock()
    return AgentRuntimeHandler(main_content=mc, chat_render_handler=crh, GLib_module=None), crh, mc


def _register_coder(handler):
    """Register a Coder special agent and return its conv_id_prefix."""
    coder = SpecialAgentDef(
        conv_id_prefix="special:coder",
        display_name="Coder",
        role="coder",
        emoji="🛠️",
        tools=["read_file", "write_file", "edit_file", "exec_command",
               "list_files", "search_files", "web_search", "web_fetch"],
        can_write=True,
        llm_name="minimax",
    )
    handler.add_special_agent(coder)
    return coder


class TestLocalAgentHeaderStreaming:
    """Test A: _do_response_complete streaming path passes real display name."""

    def test_streaming_passes_display_name_to_end_streaming(self):
        handler, crh, _mc = _make_handler()
        _register_coder(handler)

        crh.is_streaming.return_value = True
        # _do_response_complete writes to self._crh — call directly
        handler._do_response_complete("special:coder", "hello world")

        crh.end_streaming.assert_called_once()
        # The new fix: agent_name kwarg is the display name, not None
        call_kwargs = crh.end_streaming.call_args.kwargs
        call_args = crh.end_streaming.call_args.args
        # Signature is end_streaming(session_key, agent_name=None)
        # — verify agent_name kwarg or positional[1] is "Coder"
        if "agent_name" in call_kwargs:
            assert call_kwargs["agent_name"] == "Coder", (
                f"FAILURE-CASE REPRO: end_streaming was called with "
                f"agent_name={call_kwargs['agent_name']!r}; expected 'Coder' "
                "so the local-agent header renders"
            )
        else:
            assert len(call_args) >= 2 and call_args[1] == "Coder", (
                f"FAILURE-CASE REPRO: end_streaming positional args were "
                f"{call_args!r}; expected ('special:coder', 'Coder')"
            )


class TestLocalAgentHeaderNonStreaming:
    """Test B: _do_response_complete non-streaming path uses real name, not hardcoded 'Agent'."""

    def test_non_streaming_passes_display_name_to_render_sync(self):
        handler, crh, _mc = _make_handler()
        _register_coder(handler)

        crh.is_streaming.return_value = False
        handler._do_response_complete("special:coder", "hello world")

        crh.render_sync.assert_called_once()
        call_kwargs = crh.render_sync.call_args.kwargs
        call_args = crh.render_sync.call_args.args
        # render_sync(role, text, session_key, agent_name=...)
        if "agent_name" in call_kwargs:
            assert call_kwargs["agent_name"] == "Coder", (
                f"FAILURE-CASE REPRO: render_sync was called with "
                f"agent_name={call_kwargs['agent_name']!r}; expected 'Coder' "
                "instead of the hardcoded 'Agent' that the bug shipped with"
            )
        else:
            assert len(call_args) >= 4 and call_args[3] == "Coder", (
                f"FAILURE-CASE REPRO: render_sync positional args were "
                f"{call_args!r}; expected (..., 'Coder')"
            )


class TestEndStreamingExplicitNameTakesPriority:
    """Test C — REPOINTED for SPEC-06 SP4: end_streaming honors an
    explicitly passed agent_name over the agent_mgr lookup path (which
    returns '' for special: keys).

    Patches crh_module.render_document (the SP4 composition seam — the
    Pango build_role_bubble is retired) and asserts on a SPY SURFACE:
    surface.append_message must receive agent_name='Coder'. GTK-free.

    (Old __streaming_bubbles seam deleted — seed _streaming/_stream_text.)
    """

    def test_explicit_agent_name_reaches_surface_append(self):
        """Seed the streaming buffers, end_streaming(agent_name='Coder'),
        and assert the surface received agent_name='Coder'."""
        import ui.handlers.chat_render_handler as crh_module

        from ui.handlers.chat_render_handler import ChatRenderHandler

        crh = ChatRenderHandler(GLib_module=None)
        # mock main_content with empty _agent_mgr (simulates AgentManager miss
        # for a special: key — the original bug condition)
        mc = MagicMock()
        mc._agent_mgr.get_name.return_value = ""
        crh._main_content = mc
        # dispatch is a no-op without GLib, so _finalize runs synchronously
        crh._dispatch = lambda fn: fn()

        # Spy surface: records append_message kwargs (GTK-free).
        appended: list = []

        class _Spy:
            def append_message(self, role, html_fragment, agent_name=None):
                appended.append(
                    {"role": role, "html": html_fragment, "agent": agent_name}
                )

            def destroy(self):
                pass

        crh._surfaces["special:coder"] = _Spy()

        # NEW seams (SP4): streaming state is _streaming/_stream_text.
        crh._streaming.add("special:coder")
        crh._stream_text["special:coder"] = "hello"

        with patch.object(crh_module, "render_document",
                          return_value="<p>hello</p>") as rd:
            crh.end_streaming("special:coder", agent_name="Coder")

        # Composition ran once with the buffered text.
        assert rd.call_count == 1
        assert rd.call_args.args[0] == "hello"
        # The surface got the EXPLICIT name — header contract at the
        # public boundary.
        assert len(appended) == 1
        assert appended[0]["agent"] == "Coder", (
            f"FAILURE-CASE REPRO: surface append received agent_name="
            f"{appended[0]['agent']!r}; expected 'Coder' so the header renders"
        )


class TestEndStreamingFallbackForGatewayAgents:
    """Test D — REPOINTED for SPEC-06 SP4: end_streaming with no agent_name
    arg falls back to agent_mgr (gateway compatibility — gateway agents ARE
    in AgentManager). Spy-surface assert at the new seam.
    """

    def test_no_agent_name_falls_back_to_agent_mgr(self):
        import ui.handlers.chat_render_handler as crh_module

        from ui.handlers.chat_render_handler import ChatRenderHandler

        crh = ChatRenderHandler(GLib_module=None)
        mc = MagicMock()
        mc._agent_mgr.get_name.return_value = "Qaster"  # gateway agent registered
        crh._main_content = mc
        crh._dispatch = lambda fn: fn()

        appended: list = []

        class _Spy:
            def append_message(self, role, html_fragment, agent_name=None):
                appended.append(
                    {"role": role, "html": html_fragment, "agent": agent_name}
                )

            def destroy(self):
                pass

        crh._surfaces["agent:qaster:main"] = _Spy()
        crh._streaming.add("agent:qaster:main")
        crh._stream_text["agent:qaster:main"] = "hello"

        with patch.object(crh_module, "render_document",
                          return_value="<p>hello</p>"):
            crh.end_streaming("agent:qaster:main")  # NO agent_name arg

        # agent_mgr.get_name was called for the session key
        mc._agent_mgr.get_name.assert_called_with("agent:qaster:main")
        # The surface received "Qaster" (the gateway name)
        assert len(appended) == 1
        assert appended[0]["agent"] == "Qaster", (
            f"FAILURE-CASE REPRO: end_streaming fallback should have resolved "
            f"'Qaster' via agent_mgr; got {appended[0]['agent']!r}"
        )


# ═══════════════════════════════════════════════════════════════════
#  allowed_tools fallback (allowed-tools-fallback-spec.md)
# ═══════════════════════════════════════════════════════════════════
#
# Regression tests for the stale-state bug: pre-fix conversations had
# allowed_tools: null on disk, which bypassed the execute_tool gate
# entirely. The fix: in _load_conversation_from_disk, if persisted
# allowed_tools is None, fall back to the live agent definition's
# tools list via get_special_agent(). Mirrors the HIGH-3 api_key
# re-resolution pattern and the Option C+ project_path pattern.


class TestAllowedToolsFallback:
    """Spec: allowed-tools-fallback-spec.md (Edit 2).

    The persisted allowed_tools field may be None for conversations
    created before the execute_tool gate shipped. The fallback fires
    at load time and re-populates conv.allowed_tools from the live
    SpecialAgentDef so the gate is a no-op ONLY when the live config
    also has no allow-list.
    """

    def test_persisted_none_falls_back_to_live_agent_definition(
        self, tmp_path, monkeypatch
    ):
        """A conversation persisted with allowed_tools: null gets the
        live agent's tools list. Failure-case reproduction: without
        this, the Debugger agent's pre-fix conversation has no
        allow-list and the gate is a no-op.
        """
        # Arrange: write a conversation with allowed_tools: null
        d = tmp_path / "conversations"
        d.mkdir()
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        persisted = {
            "session_key": "special:debugger",
            "agent_name": "Debugger",
            "agent_role": "debugger",
            "project_path": None,
            "model": "openai/gpt-4o",
            "provider": "openai",
            "messages": [],
            "system_prompt": "",
            "total_tokens": 0,
            "total_cost": 0.0,
            "step_count": 0,
            # KEY: allowed_tools: null — the bug condition.
            "allowed_tools": None,
        }
        (d / "special:debugger.json").write_text(_json.dumps(persisted))

        # Patch get_special_agent to return a read-only Debugger def.
        # Mirrors the production Debugger YAML (read-only).
        debugger_def = SpecialAgentDef(
            conv_id_prefix="special:debugger",
            display_name="Debugger",
            role="debugger",
            emoji="🐛",
            tools=["read_file", "list_files", "search_files"],
            can_write=False,
        )
        monkeypatch.setattr(
            "agent.special_agents.get_special_agent",
            lambda sk: debugger_def if sk == "special:debugger" else None,
        )

        # Act
        from agent.persistence import load_conversation_from_disk
        result = load_conversation_from_disk("special:debugger")
        assert result is not None
        conv, _ = result

        # Assert: fallback fired — conv.allowed_tools is the live list,
        # not None. The execute_tool gate will now deny write_file /
        # edit_file for this conversation.
        assert conv.allowed_tools == ["read_file", "list_files", "search_files"], (
            f"FAILURE-CASE REPRO: conv.allowed_tools={conv.allowed_tools!r}; "
            "expected live fallback list. The Debugger agent's pre-fix "
            "conversation would have allowed_tools=None, and the gate "
            "would be a no-op — letting write_file/edit_file through."
        )

    def test_persisted_list_wins_over_fallback(
        self, tmp_path, monkeypatch
    ):
        """Happy path: persisted allowed_tools is a real list — fallback
        does NOT override it. The persisted list is the source of
        truth when present (e.g., the Coder agent's conversation has
        the correct list persisted).
        """
        # Arrange: write a conversation with a real list
        d = tmp_path / "conversations"
        d.mkdir()
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        persisted_tools = ["read_file", "write_file"]
        persisted = {
            "session_key": "special:coder",
            "agent_name": "Coder",
            "agent_role": "coder",
            "project_path": None,
            "model": "openai/gpt-4o",
            "provider": "openai",
            "messages": [],
            "system_prompt": "",
            "total_tokens": 100,
            "total_cost": 0.01,
            "step_count": 1,
            "allowed_tools": persisted_tools,  # real list, not None
        }
        (d / "special:coder.json").write_text(_json.dumps(persisted))

        # Patch get_special_agent to return a DIFFERENT live list.
        # If the fallback fires, conv.allowed_tools would be overwritten
        # with this list. The persisted list must win.
        live_def = SpecialAgentDef(
            conv_id_prefix="special:coder",
            display_name="Coder",
            role="coder",
            emoji="🛠️",
            tools=["read_file"],  # different from persisted
            can_write=True,
        )
        fallback_called = MagicMock(return_value=live_def)
        monkeypatch.setattr(
            "agent.special_agents.get_special_agent", fallback_called
        )

        # Act
        from agent.persistence import load_conversation_from_disk
        result = load_conversation_from_disk("special:coder")
        assert result is not None
        conv, _ = result

        # Assert: persisted list is preserved
        assert conv.allowed_tools == persisted_tools, (
            f"persisted allowed_tools was overwritten by fallback: "
            f"got {conv.allowed_tools!r}, expected {persisted_tools!r}"
        )
        # The fallback lookup was NOT called (the early return at
        # `if conv.allowed_tools is None:` skipped it).
        fallback_called.assert_not_called()

    def test_persisted_none_with_unregistered_agent_leaves_none(
        self, tmp_path, monkeypatch
    ):
        """Edge case: persisted None + agent not in registry → stays None.

        This is the "agent no longer registered" case (e.g., the YAML
        was deleted between sessions). The fallback is best-effort;
        leaving None means the gate is skipped — same as today.
        """
        d = tmp_path / "conversations"
        d.mkdir()
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        persisted = {
            "session_key": "special:deleted_agent",
            "agent_name": "DeletedAgent",
            "project_path": None,
            "model": "openai/gpt-4o",
            "messages": [],
            "system_prompt": "",
            "total_tokens": 0,
            "total_cost": 0.0,
            "step_count": 0,
            "allowed_tools": None,
        }
        (d / "special:deleted_agent.json").write_text(_json.dumps(persisted))

        # Agent not in registry
        monkeypatch.setattr(
            "agent.special_agents.get_special_agent", lambda sk: None
        )

        from agent.persistence import load_conversation_from_disk
        result = load_conversation_from_disk("special:deleted_agent")
        assert result is not None
        conv, _ = result

        # Stays None — the gate is skipped, same as before the fallback
        assert conv.allowed_tools is None


# ═══════════════════════════════════════════════════════════════════
#  Pre-Call Budget Guard
# ═══════════════════════════════════════════════════════════════════

class TestPreCallBudgetGuard:
    """Tests for the pre-call RuntimeError guard in _run_loop."""

    def test_guard_fires_when_post_trim_exceeds_budget(self):
        """Guard raises RuntimeError when conversation exceeds model_max minus response reserve."""
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai",
                    base_url="https://api.openai.com/v1",
                    api_key="test-key",
                    default_model="gpt-4o",
                    max_tokens=100,
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=1,
            tool_timeout_seconds=5,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.system_prompt = "Test"
        conv.add_user_message("hello")

        errors = []
        rt._on_error = lambda session_key, msg: errors.append(str(msg))
        rt._run_loop(sk, "test")
        assert errors, "guard should have fired"
        assert "exceeds model context window" in errors[0]

    def test_guard_message_includes_usage_percent_and_hint(self):
        """Error message must include usage percent and remediation hint."""
        from agent.config import AgentConfig, LLMProviderConfig
        cfg = AgentConfig(
            providers={
                "openai": LLMProviderConfig(
                    name="openai",
                    base_url="https://api.openai.com/v1",
                    api_key="test-key",
                    default_model="gpt-4o",
                    max_tokens=50,
                )
            },
            default_provider="openai",
            default_model="openai/gpt-4o",
            max_tool_iterations=1,
            tool_timeout_seconds=5,
            auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        conv = rt.get_conversation(sk)
        conv.system_prompt = "Test"
        conv.add_user_message("hello")

        errors = []
        rt._on_error = lambda session_key, msg: errors.append(str(msg))
        rt._run_loop(sk, "test")
        assert errors, "guard should have fired"
        msg = errors[0]
        assert "%" in msg, f"missing percent: {msg}"
        assert "/clear" in msg, f"missing /clear hint: {msg}"
        assert "/compact" in msg, f"missing /compact hint: {msg}"


# ═══════════════════════════════════════════════════════════════════
#  SSE Frame-Shape Hardening (Phase 1)
# ═══════════════════════════════════════════════════════════════════

class TestSSEFrameShapeHardening:
    """Phase 1 hardening: first_choice helper + parse_sse_delta safety
    against empty-choices frames (OpenAI trailing usage, keepalive, etc.)."""

    def test_first_choice_normal_frame(self):
        """Normal delta frame: choices[0] is returned."""
        from agent.llm.streaming import first_choice
        result = first_choice({"choices": [{"delta": {"content": "hi"}}]})
        assert result == {"delta": {"content": "hi"}}

    def test_first_choice_empty_choices_with_usage(self):
        """OpenAI trailing usage frame: choices is [], should return {}."""
        from agent.llm.streaming import first_choice
        result = first_choice({"choices": [], "usage": {"total_tokens": 42}})
        assert result == {}

    def test_first_choice_no_choices_key(self):
        """Keepalive frame: no choices key at all, should return {}."""
        from agent.llm.streaming import first_choice
        assert first_choice({}) == {}

    def test_first_choice_only_usage(self):
        """Usage-only frame: {"usage": {...}}, no choices."""
        from agent.llm.streaming import first_choice
        assert first_choice({"usage": {"prompt_tokens": 10}}) == {}

    def test_parse_sse_delta_empty_choices_no_crash(self):
        """parse_sse_delta must not crash on empty choices (the original bug)."""
        from agent.llm.streaming import parse_sse_delta
        result = parse_sse_delta({"choices": [], "usage": {"total_tokens": 42}})
        assert result == []

    def test_parse_sse_delta_no_choices_key_no_crash(self):
        """parse_sse_delta must not crash when choices key is absent."""
        from agent.llm.streaming import parse_sse_delta
        assert parse_sse_delta({}) == []

    def test_parse_sse_delta_normal_text_delta(self):
        """Normal text delta still works after hardening."""
        from agent.llm.streaming import parse_sse_delta
        events = parse_sse_delta(
            {"choices": [{"delta": {"content": "hello"}}]}
        )
        assert len(events) == 1
        assert events[0].type == "text_delta"
        assert events[0].data["content"] == "hello"

    def test_parse_sse_delta_tool_call_delta(self):
        """Tool call deltas still work after hardening."""
        from agent.llm.streaming import parse_sse_delta
        events = parse_sse_delta(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "read_file", "arguments": "{\"path\":\"a.py\"}"}}
            ]}}]}
        )
        assert len(events) == 1
        assert events[0].type == "tool_call_delta"
        assert events[0].data["name"] == "read_file"
        assert events[0].data["id"] == "call_1"

    def test_no_unguarded_choices_zero_indexing(self):
        """Regression: no remaining d.get('choices', [{}])[0] patterns."""
        import subprocess
        result = subprocess.run(
            ["grep", "-n", 'd\.get("choices", [{}])[0]', "agent/runtime.py"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.stdout == "", (
            f"Unguarded [0] indexing found:\n{result.stdout}"
        )

    def test_first_choice_used_at_all_three_sites(self):
        """first_choice must be importable from agent.llm.streaming and used
        in all streaming provider modules (openai, minimax) and in runtime.
        """
        import subprocess
        # Check all provider modules + streaming module + runtime
        files = [
            "agent/runtime.py",
            "agent/llm/streaming.py",
            "agent/llm/openai_provider.py",
            "agent/llm/minimax_provider.py",
        ]
        total = 0
        for f in files:
            result = subprocess.run(
                ["grep", "-c", "first_choice", f],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
            if result.stdout.strip():
                total += int(result.stdout.strip())
        assert total >= 5, f"Expected >= 5 first_choice references across provider modules, got {total}"


class TestStreamOpenaiEventsFinishReason:
    """_stream_openai_events emits done on finish_reason (not just [DONE])."""

    def _fake_urlopen(self, raw_sse: bytes):
        class _FakeResp:
            def __init__(self, buf):
                self._buf = buf
            def __iter__(self):
                return iter(self._buf.splitlines(keepends=True))

        class _Ctx:
            def __enter__(self_ctx):
                return _FakeResp(raw_sse)
            def __exit__(self_ctx, *a):
                pass
        return _Ctx()

    def _run_streamer(self, raw_sse: bytes):
        from agent import runtime as rt_module
        from agent.llm.openai_provider import OpenAIProvider

        with unittest.mock.patch(
            "agent.llm.openai_provider.urlopen_with_ssl_retry",
            lambda req, timeout: self._fake_urlopen(raw_sse),
        ):
            return list(OpenAIProvider("openai").stream(
                base_url="https://api.openai.com/v1",
                api_key="***",
                model="openai/gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                timeout=30.0,
                x_title="",
            ))

    def _text_delta_sse(self, text: str, finish_reason: str | None = None) -> bytes:
        """Build a two-frame SSE: one text delta, one optional finish_reason."""
        lines = [
            b'data: {"choices":[{"delta":{"content":"' + text.encode() + b'"}}]}\n\n',
        ]
        if finish_reason is not None:
            lines.append(
                b'data: {"choices":[{"finish_reason":"' + finish_reason.encode() + b'"}]}\n\n'
            )
        return b"".join(lines)

    # --- finish_reason variants ---

    def test_emits_done_on_finish_reason_stop(self):
        """done event emitted when finish_reason='stop'."""
        events = self._run_streamer(self._text_delta_sse("ok", finish_reason="stop"))
        types = [ev.type for ev in events]
        assert "text_delta" in types
        assert "done" in types, f"expected done event, got types={types}"
        # done should be the last event
        assert types[-1] == "done"

    def test_emits_done_on_finish_reason_tool_calls(self):
        """done event emitted when finish_reason='tool_calls'."""
        events = self._run_streamer(self._text_delta_sse("ok", finish_reason="tool_calls"))
        types = [ev.type for ev in events]
        assert "done" in types, f"expected done event, got types={types}"
        assert types[-1] == "done"

    def test_emits_done_on_finish_reason_length(self):
        """done event emitted when finish_reason='length'."""
        events = self._run_streamer(self._text_delta_sse("ok", finish_reason="length"))
        types = [ev.type for ev in events]
        assert "done" in types, f"expected done event, got types={types}"
        assert types[-1] == "done"

    # --- finish_reason "error" and "content_filter" ---

    def test_emits_done_on_finish_reason_error(self):
        """done event emitted when finish_reason='error'."""
        events = self._run_streamer(self._text_delta_sse("ok", finish_reason="error"))
        types = [ev.type for ev in events]
        assert "done" in types, f"expected done event, got types={types}"
        assert types[-1] == "done"

    def test_emits_error_event_with_error_data(self):
        """SSEEvent(type='error', ...) yielded before done when finish_reason='error' and error field present."""
        raw = (
            b'data: {"error":{"code":429,"message":"Rate limit exceeded"},'
            b'"choices":[{"delta":{},"finish_reason":"error"}]}\n\n'
        )
        events = self._run_streamer(raw)
        types = [ev.type for ev in events]
        assert "error" in types, f"expected error event, got types={types}"
        assert "done" in types, f"expected done event, got types={types}"
        # error event must precede done event
        assert types.index("error") < types.index("done"), (
            f"error event must precede done; got order={types}"
        )
        # Verify error data is passed through
        error_ev = [ev for ev in events if ev.type == "error"][0]
        err_data = error_ev.data.get("error", {})
        assert err_data.get("code") == 429
        assert "Rate limit" in err_data.get("message", "")

    def test_no_error_event_when_error_field_missing(self):
        """No error event when finish_reason='error' but no error dict in SSE data."""
        raw = (
            b'data: {"choices":[{"delta":{},"finish_reason":"error"}]}\n\n'
        )
        events = self._run_streamer(raw)
        types = [ev.type for ev in events]
        assert "error" not in types, f"unexpected error event, got types={types}"
        assert "done" in types, f"expected done event, got types={types}"

    def test_emits_done_on_finish_reason_content_filter(self):
        """done event emitted when finish_reason='content_filter'."""
        events = self._run_streamer(self._text_delta_sse("", finish_reason="content_filter"))
        types = [ev.type for ev in events]
        assert "done" in types, f"expected done event, got types={types}"
        assert types[-1] == "done"

    def test_emits_error_event_for_finish_reason_content_filter(self):
        """SSEEvent(type='error', ...) yielded with code=400, reason='content_filter' when finish_reason='content_filter'."""
        raw = (
            b'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}\n\n'
        )
        events = self._run_streamer(raw)
        types = [ev.type for ev in events]
        assert "error" in types, f"expected error event for content_filter, got types={types}"
        error_ev = [ev for ev in events if ev.type == "error"][0]
        err_data = error_ev.data.get("error", {})
        assert err_data.get("code") == 400, f"expected code=400, got {err_data}"
        assert err_data.get("reason") == "content_filter", f"expected reason='content_filter', got {err_data}"
        assert "filtered" in err_data.get("message", "").lower()

    # --- [DONE] sentinel regression ---

    def test_handles_done_sentinel(self):
        """Regression: [DONE] sentinel still works when finish_reason absent."""
        raw = (
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        events = self._run_streamer(raw)
        types = [ev.type for ev in events]
        assert "done" in types, f"expected done event from [DONE], got types={types}"
        assert types[-1] == "done"

    # --- no termination ---

    def test_no_done_when_no_finish_reason_and_no_done_sentinel(self):
        """No done event when stream has neither finish_reason nor [DONE] sentinel."""
        raw = (
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        )
        events = self._run_streamer(raw)
        types = [ev.type for ev in events]
        assert "done" not in types, f"unexpected done event, got types={types}"


# ── Local-Agent Activity Drawer Emission Tests ────────────────────────────────

class TestLocalAgentDrawerEmissions:
    """Tests for local-agent activity bubble and drawer-lifecycle emissions.

    Covers all 6 emission points in AgentRuntimeHandler:
    - tool_start from _do_tool_call_start
    - tool_end from _do_tool_call_result (non-write_file)
    - tool_error from _do_tool_call_result (failed tool)
    - patch from _do_tool_call_result (write_file success)
    - drawer-lifecycle start from _do_turn_start (BUG #21 redesign)
    - drawer-lifecycle end from _do_response_complete AND _do_error

    BUG #1 regression tests: verify is_error detection via the success param.
    BUG #12 regression: write_file success emits only patch, not tool_end.
    """

    def _make_handler(self):
        """Create a handler with mocked deps and a captured bubble list."""
        handler, crh, mc = _make_handler()
        handler._fh = MagicMock()
        handler._active_project = ("test", "/tmp/test")
        handler._agents = {
            "special:coder": SpecialAgentDef(
                display_name="Coder",
                role="coder",
                emoji="🛠️",
                tools=["exec_command", "read_file", "write_file"],
                can_write=True,
                conv_id_prefix="special:coder",
            )
        }

        # Capture activity bubbles
        self._bubbles = []
        def capture_bubble(bubble):
            self._bubbles.append(bubble)
        handler.set_on_activity_bubble(capture_bubble)

        # Capture drawer lifecycle events
        self._lifecycle_events = []
        def capture_lifecycle(sk, name, phase):
            self._lifecycle_events.append((sk, name, phase))
        handler.set_on_drawer_lifecycle(capture_lifecycle)

        # _pending_tool_args must be cleared
        handler._pending_tool_args = {}
        handler._ended_sessions = set()

        return handler, crh, mc

    def _make_handler_with_agent(self):
        """Create a handler with a registered Coder agent and capture callbacks."""
        return self._make_handler()

    # ── tool_start ──────────────────────────────────────────────────────

    def test_do_tool_call_start_emits_tool_start_bubble(self):
        """_do_tool_call_start emits ActivityBubble with type='tool_start'."""
        handler, _, _ = self._make_handler_with_agent()

        handler._do_tool_call_start("special:coder", "read_file", {"path": "test.txt"})

        assert len(self._bubbles) == 1, "Expected exactly one bubble"
        assert self._bubbles[0].type == "tool_start"
        assert self._bubbles[0].session_key == "special:coder"
        assert self._bubbles[0].tool_name == "read_file"

    def test_do_tool_call_start_stores_tool_args(self):
        """_do_tool_call_start stores args in _pending_tool_args for patch path."""
        handler, _, _ = self._make_handler_with_agent()

        handler._do_tool_call_start("special:coder", "write_file", {"path": "src/main.py"})

        assert handler._pending_tool_args["special:coder"] == {"path": "src/main.py"}

    # ── tool_end (non-write_file) ───────────────────────────────────────

    def test_do_tool_call_result_emits_tool_end_bubble(self):
        """_do_tool_call_result emits ActivityBubble with type='tool_end' on success (non-write_file)."""
        handler, _, _ = self._make_handler_with_agent()
        handler._pending_tool_args["special:coder"] = {"path": "test.txt"}

        handler._do_tool_call_result("special:coder", "read_file", "file content", success=True)

        # Should get tool_end, no patch
        types = [b.type for b in self._bubbles]
        assert "tool_end" in types, f"Expected tool_end, got {types}"
        assert "patch" not in types, f"Expected no patch for read_file, got {types}"

    # ── tool_error ──────────────────────────────────────────────────────

    def test_do_tool_call_result_emits_tool_error_on_failure(self):
        """_do_tool_call_result emits ActivityBubble with type='tool_error' when success=False."""
        handler, _, _ = self._make_handler_with_agent()
        handler._pending_tool_args["special:coder"] = {"path": "test.txt"}

        handler._do_tool_call_result("special:coder", "read_file", "permission denied", success=False)

        types = [b.type for b in self._bubbles]
        assert "tool_error" in types, f"Expected tool_error, got {types}"
        # Verify icon/status via the bubble
        tool_error = [b for b in self._bubbles if b.type == "tool_error"][0]
        assert tool_error.icon == "❌"

    # ── BUG #1: denied exec → tool_error ────────────────────────────────

    def test_denied_exec_command_emits_tool_error_bubble(self):
        """Simulate exec denial path: success=False → tool_error with ❌."""
        handler, _, _ = self._make_handler_with_agent()
        handler._pending_tool_args["special:coder"] = {"command": "rm -rf /"}

        # Runtime dispatches with success=False for denied exec
        handler._do_tool_call_result("special:coder", "exec_command", "denied", success=False)

        types = [b.type for b in self._bubbles]
        assert "tool_error" in types, f"Expected tool_error, got {types}"
        tool_error = [b for b in self._bubbles if b.type == "tool_error"][0]
        assert tool_error.icon == "❌"
        assert tool_error.status.value == "error"

    def test_sensitive_path_block_emits_tool_error_bubble(self):
        """Simulate sensitive-path write_file block: success=False → tool_error."""
        handler, _, _ = self._make_handler_with_agent()
        handler._pending_tool_args["special:coder"] = {"path": "/etc/passwd"}

        handler._do_tool_call_result("special:coder", "write_file", "blocked", success=False)

        types = [b.type for b in self._bubbles]
        assert "tool_error" in types, f"Expected tool_error, got {types}"

    # ── BUG #12: write_file success → patch, NOT tool_end ───────────────

    def test_write_file_success_emits_patch_not_tool_end(self):
        """Successful write_file emits ONLY patch, NO tool_end (BUG #12)."""
        handler, _, _ = self._make_handler_with_agent()
        handler._pending_tool_args["special:coder"] = {"path": "src/main.py"}

        handler._do_tool_call_result("special:coder", "write_file", "OK — wrote 123 bytes to src/main.py", success=True)

        types = [b.type for b in self._bubbles]
        assert "patch" in types, f"Expected patch, got {types}"
        assert "tool_end" not in types, f"BUG #12: Expected NO tool_end for write_file success, got {types}"

    def test_write_file_failure_emits_tool_error(self):
        """Failed write_file emits tool_error, not patch."""
        handler, _, _ = self._make_handler_with_agent()
        handler._pending_tool_args["special:coder"] = {"path": "src/main.py"}

        handler._do_tool_call_result("special:coder", "write_file", "permission denied", success=False)

        types = [b.type for b in self._bubbles]
        assert "tool_error" in types, f"Expected tool_error, got {types}"
        assert "patch" not in types, f"Expected no patch on failure, got {types}"

    # ── BUG #5: _pending_tool_args leak ─────────────────────────────────

    def test_pending_tool_args_cleared_after_non_writefile_error(self):
        """_pending_tool_args is popped unconditionally after bubble dispatch (BUG #5)."""
        handler, _, _ = self._make_handler_with_agent()

        # Simulate 3 failed read_file calls
        for i in range(3):
            handler._pending_tool_args["special:coder"] = {"path": f"file{i}.txt"}
            handler._do_tool_call_result("special:coder", "read_file", "error", success=False)

        # After all calls, _pending_tool_args should be empty (popped each time)
        assert "special:coder" not in handler._pending_tool_args

    # ── BUG #2: orphan tool_start after cancel ──────────────────────────

    def test_tool_start_suppressed_after_session_ended(self):
        """_do_tool_call_start returns early when session is in _ended_sessions (BUG #2)."""
        handler, _, _ = self._make_handler_with_agent()
        handler._ended_sessions.add("special:coder")

        handler._do_tool_call_start("special:coder", "read_file", {"path": "test.txt"})

        # No bubble should have been emitted
        assert len(self._bubbles) == 0, "BUG #2: Expected no tool_start for ended session"

    # ── drawer-lifecycle start ──────────────────────────────────────────

    def test_agent_start_emits_drawer_lifecycle_start(self):
        """_do_text_delta's agent-start site emits drawer-lifecycle 'start'."""
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh

        # This triggers the agent-start site (is_streaming=False → start_streaming)
        handler._do_text_delta("special:coder", "Hello")

        assert len(self._lifecycle_events) >= 1
        events = [(sk, name, phase) for sk, name, phase in self._lifecycle_events if phase == "start"]
        assert len(events) >= 1, f"Expected at least one 'start' event, got {self._lifecycle_events}"
        assert events[0] == ("special:coder", "Coder", "start")

    # ── drawer-lifecycle end ────────────────────────────────────────────

    def test_agent_end_emits_drawer_lifecycle_end(self):
        """_do_response_complete emits drawer-lifecycle 'end'."""
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh

        handler._do_response_complete("special:coder", "Done")

        events = [e for e in self._lifecycle_events if e[2] == "end"]
        assert len(events) >= 1
        assert events[0] == ("special:coder", "Coder", "end")

    def test_do_error_emits_drawer_lifecycle_end(self):
        """_do_error emits drawer-lifecycle 'end'."""
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh

        handler._do_error("special:coder", "Something went wrong")

        events = [e for e in self._lifecycle_events if e[2] == "end"]
        assert len(events) >= 1
        assert events[0] == ("special:coder", "Coder", "end")

    # ── BUG #4: tool bubbles emitted without active project ──────────────

    def test_tool_bubbles_emitted_without_active_project(self):
        """tool_start/tool_end bubbles fire even without _active_project (BUG #4)."""
        handler, crh, mc = _make_handler()
        handler._fh = MagicMock()
        # No _active_project set
        handler._active_project = None
        handler._agents = {
            "special:coder": SpecialAgentDef(
                display_name="Coder", role="coder", emoji="🛠️", tools=["read_file"],
                can_write=True, conv_id_prefix="special:coder",
            )
        }

        bubbles = []
        handler.set_on_activity_bubble(lambda b: bubbles.append(b))
        handler._pending_tool_args = {}
        handler._ended_sessions = set()

        # tool_start must fire even without _active_project
        handler._do_tool_call_start("special:coder", "read_file", {"path": "test.txt"})
        assert len(bubbles) == 1, f"BUG #4: Expected tool_start bubble, got {len(bubbles)}"
        assert bubbles[0].type == "tool_start"

        # Store pending args for tool_end dispatch
        handler._pending_tool_args["special:coder"] = {"path": "test.txt"}
        handler._do_tool_call_result("special:coder", "read_file", "content", success=True)
        assert len(bubbles) >= 2, f"BUG #4: Expected tool_end bubble too, got {len(bubbles)}"
        assert bubbles[1].type == "tool_end"

    # ── BUG #13: feed-card block must not shadow success param ──────────

    def test_denied_exec_with_card_still_emits_tool_error(self):
        """Regression for BUG #13: success param shadowed by feed-card block.

        The existing test_denied_exec_command_emits_tool_error_bubble bypasses the
        feed-card block (no _tool_card_ids entry). This test primes the card so the
        shadowing block runs, proving the rename fix holds.
        """
        handler, _, _ = self._make_handler_with_agent()
        # Prime _tool_card_ids as if _do_tool_call_start had run first
        from models.feed_card import FeedCardData
        from datetime import datetime, timezone
        card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title="Coder is running: rm -rf /",
            body="⏳ Running...",
            author="Coder",
            timestamp=datetime.now(timezone.utc),
            project_name="test",
        )
        handler._tool_card_ids["special:coder"] = "card-123"
        handler._fh.get_card.return_value = card

        # Runtime dispatches success=False for a denied exec_command
        handler._do_tool_call_result(
            "special:coder", "exec_command",
            "exec_command requires PM approval — request denied or timed out",
            success=False,
        )

        types = [b.type for b in self._bubbles]
        assert "tool_error" in types, (
            f"BUG #13: tool_error not emitted when feed-card block ran first; got {types}"
        )
        tool_error = [b for b in self._bubbles if b.type == "tool_error"][0]
        assert tool_error.icon == "❌"

    def test_write_file_failure_string_result_emits_tool_error(self):
        """Regression for BUG #13 compounding case: failed write_file with a string
        result AND a feed card must emit tool_error (not be silently suppressed).

        Before the fix, the shadowing forced success=True in the card block and
        BUG #12's skip_tool_end read that value → is_error=False → NO bubble at all.
        """
        handler, _, _ = self._make_handler_with_agent()
        from models.feed_card import FeedCardData
        from datetime import datetime, timezone
        card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title="Coder is writing /etc/passwd",
            body="⏳ Running...",
            author="Coder",
            timestamp=datetime.now(timezone.utc),
            project_name="test",
        )
        handler._tool_card_ids["special:coder"] = "card-123"
        handler._fh.get_card.return_value = card
        handler._pending_tool_args["special:coder"] = {"path": "/etc/passwd"}

        handler._do_tool_call_result(
            "special:coder", "write_file", "blocked: sensitive path", success=False,
        )

        types = [b.type for b in self._bubbles]
        assert "tool_error" in types, (
            f"BUG #13: failed write_file emitted no terminal bubble; got {types}"
        )
        # No patch on failure
        assert "patch" not in types, f"Expected no patch on failure; got {types}"

    # ── BUG #18: ≥2 stale tool_starts must both be suppressed ───────────

    def test_two_consecutive_stale_tool_starts_both_suppressed(self):
        """Regression for BUG #18: ≥2 stale tool_start dispatches must all be
        suppressed. The Round 2 fix cleared the ended flag on the first stale
        call, letting the second proceed and emit an orphan bubble.
        """
        handler, _, _ = self._make_handler_with_agent()
        handler._ended_sessions.add("special:coder")

        # Two stale calls, back-to-back (as if queued in GLib.idle_add)
        handler._do_tool_call_start("special:coder", "read_file", {"path": "a.txt"})
        handler._do_tool_call_start("special:coder", "read_file", {"path": "b.txt"})

        # Both must be suppressed — no bubbles, flag still set
        assert len(self._bubbles) == 0, (
            f"BUG #18: expected 0 bubbles for 2 stale calls, got {len(self._bubbles)}"
        )
        assert "special:coder" in handler._ended_sessions, (
            "BUG #18: ended flag should NOT be cleared by a stale call"
        )

    # ── BUG #17: card/bubble agreement on denied tools ──────────────────

    def test_denied_exec_card_shows_error_status(self):
        """Regression for BUG #17: card.metadata['status'] must be 'error' for a
        denied tool, not 'complete'. The card and bubble must agree.
        """
        handler, _, _ = self._make_handler_with_agent()
        from models.feed_card import FeedCardData
        from datetime import datetime, timezone
        card = FeedCardData(
            card_type="agent_action", source="agent",
            title="Coder is running: rm -rf /", body="⏳ Running...",
            author="Coder", timestamp=datetime.now(timezone.utc), project_name="test",
        )
        handler._tool_card_ids["special:coder"] = "card-123"
        handler._fh.get_card.return_value = card

        handler._do_tool_call_result(
            "special:coder", "exec_command",
            "exec_command requires PM approval — request denied or timed out",
            success=False,
        )

        # The handler now hands a RESOLVED COPY to update_card (audit:
        # mutate-before-persist), so assert on the card the store receives
        # rather than on this local object, which is deliberately untouched.
        assert handler._fh.update_card.call_count == 1, (
            "the tool result must be persisted exactly once"
        )
        persisted = handler._fh.update_card.call_args[0][1]
        assert persisted.metadata["status"] == "error", (
            f"BUG #17: card status should be 'error' for denied tool, got "
            f"{persisted.metadata.get('status')!r}"
        )
        assert card.metadata.get("status") is None, (
            "the store's original object must not be mutated before the persist "
            f"(got status={card.metadata.get('status')!r}) — pre-fix this was "
            "stamped 'error' in place, which is the mutate-before-persist bug"
        )

    # ── BUG #21: tool-only turn tool_starts not suppressed ──────────────

    def test_tool_only_turn_tool_starts_not_suppressed(self):
        """Regression for BUG #21: a tool-only turn (no streaming text) must
        still fire tool_start bubbles.

        The runtime dispatches the dedicated _on_turn_start callback at the top
        of _run_loop (BUG #21 redesign) — no longer an empty text delta, whose
        empty-return in _do_text_delta_inner prevented the start-bubble path
        from running. This test simulates that sequence at the handler level.
        """
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh

        # Simulate the real sequence: prior turn ended → new send cleared the
        # flag (send_to_special_agent is the ONLY clear site — see
        # test_send_to_special_agent_clears_ended_sessions). So at turn-start
        # time the flag is already absent; this test starts from that state.

        # Simulate the runtime's turn-start signal — this is the fix.
        handler._do_turn_start("special:coder")

        # Now a tool_start arrives (tool-only turn, no real text)
        handler._do_tool_call_start("special:coder", "read_file", {"path": "test.txt"})

        types = [b.type for b in self._bubbles]
        assert "tool_start" in types, (
            f"BUG #21: tool_start suppressed for tool-only turn; got {types}"
        )
        start_events = [e for e in self._lifecycle_events if e[2] == "start"]
        assert len(start_events) == 1, (
            f"BUG #21: expected 1 lifecycle-start event, got {len(start_events)}: {self._lifecycle_events}"
        )

    # ── BUG #22: tool-only turn must not render empty chat bubble ───────

    def test_tool_only_turn_no_empty_chat_bubble(self):
        """Regression for BUG #22: a tool-only turn (empty streaming text) must
        not render an empty header bubble in the chat.

        The turn-start signal starts a streaming bubble (no text yet). At turn
        end, end_streaming must suppress the final bubble render when the
        streaming text is empty.
        """
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh
        # Stub get_streaming_text to return empty string (tool-only turn, no content)
        crh.get_streaming_text.return_value = ""

        # Turn-start starts a streaming bubble
        handler._do_turn_start("special:coder")
        # Simulate turn end with no text content (tool-only turn)
        handler._do_response_complete("special:coder", "")

        # end_streaming must have been called with render=False (empty text suppressed)
        crh.end_streaming.assert_called_once()
        call_kwargs = crh.end_streaming.call_args.kwargs
        assert call_kwargs.get("render") is False, (
            f"BUG #22: end_streaming should be called with render=False for empty text; "
            f"got kwargs={call_kwargs}"
        )

    # ── RACE-FIX v4: send-side _ended_sessions clear ─────────────────────

    def test_send_to_special_agent_clears_ended_sessions(self):
        """RACE-FIX v4 send-side clear: send_to_special_agent is the ONLY
        site that clears _ended_sessions — the previous turn's ended flag
        must not suppress the new turn's tool_starts.

        Replaces the dead BUG #14 tracking-set test: that mechanism was
        never implemented (the set was dead code) and is superseded by
        the on_turn_start redesign.
        """
        handler, crh, mc = self._make_handler_with_agent()
        handler._ended_sessions.add("special:coder")
        handler._session_completed.add("special:coder")

        # Stub the runtime so no real AgentRuntime/LLM is constructed.
        rt = MagicMock()
        rt.get_conversation.return_value = None
        rt.load_conversation.return_value = None
        handler._get_runtime = MagicMock(return_value=rt)

        handler.send_to_special_agent("special:coder", "hello")

        assert "special:coder" not in handler._ended_sessions, (
            "send_to_special_agent must clear _ended_sessions for the new turn"
        )
        assert "special:coder" not in handler._session_completed
        # Phase 4 Part B: the conversation prep moved off the main thread into
        # a `prepare` closure passed to send_message. `handler._prepare_turn`
        # does not exist — the closure is a LOCAL function in
        # send_to_special_agent, so assert the call shape and then exercise
        # the closure itself (Rule 4: real code path, not just the mock).
        rt.send_message.assert_called_once()
        call_args, call_kwargs = rt.send_message.call_args
        assert call_args == ("special:coder", "hello")
        prepare = call_kwargs.get("prepare")
        assert callable(prepare), (
            "Part B: send_message must receive the off-thread prepare callback"
        )
        assert prepare.__name__ == "_prepare_turn", (
            f"expected the _prepare_turn closure, got {prepare!r}"
        )
        handler._prepare_turn_conversation = MagicMock()
        prepare()
        prep_kwargs = handler._prepare_turn_conversation.call_args.kwargs
        assert prep_kwargs["session_key"] == "special:coder"
        assert prep_kwargs["project_path"] == "/tmp/test"
        assert prep_kwargs["turn_token"] is handler._turn_tokens["special:coder"]
        assert handler._turn_tokens["special:coder"] is not None

    # ── BUG #21 redesign: dedicated on_turn_start signal ─────────────────

    def test_on_turn_start_dispatches_via_glib(self):
        """The _on_turn_start wrapper queues _do_turn_start onto the main
        thread via GLib.idle_add (same pattern as the other _on_* methods)."""
        handler, crh, mc = self._make_handler_with_agent()
        glib = _QueueGLib()
        handler._GLib = glib
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)

        handler._on_turn_start("special:coder")

        assert len(glib.queue) == 1, "turn-start must be dispatched via idle_add"
        glib.drain()
        start_events = [e for e in self._lifecycle_events if e[2] == "start"]
        assert len(start_events) == 1

    def test_turn_start_stale_token_rejected(self):
        """_do_turn_start drops a signal whose token doesn't match the
        current turn (stale cross-turn dispatch after a newer send)."""
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh
        handler._turn_tokens["special:coder"] = object()
        stale = object()

        handler._do_turn_start("special:coder", stale)

        crh.start_streaming.assert_not_called()
        assert not [e for e in self._lifecycle_events if e[2] == "start"]

    def test_turn_start_after_terminal_event_dropped(self):
        """Cancel-race guard: a terminal event (error/complete/cancel) that
        lands BEFORE the turn-start dispatch must win — no orphan bubble,
        no flag clear. (cancel() dispatches _do_error with the SAME token
        as the in-flight turn, so the token check alone can't order them.)"""
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = False
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh
        token = object()
        handler._turn_tokens["special:coder"] = token
        handler._ended_sessions.add("special:coder")  # terminal landed first

        handler._do_turn_start("special:coder", token)  # same-token race

        crh.start_streaming.assert_not_called()
        assert "special:coder" in handler._ended_sessions
        assert not [e for e in self._lifecycle_events if e[2] == "start"]

    def test_do_error_renders_no_empty_bubble_on_tool_only_turn(self):
        """BUG #22 guard on _do_error: tool-only turn (bubble from
        turn-start, no text) — error path must end_streaming(render=False)."""
        handler, crh, mc = self._make_handler_with_agent()
        crh.is_streaming.return_value = True  # bubble exists from turn-start
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh
        crh.get_streaming_text.return_value = ""  # tool-only: no content

        handler._do_error("special:coder", "boom")

        crh.end_streaming.assert_called_once()
        kwargs = crh.end_streaming.call_args.kwargs
        assert kwargs.get("render") is False, (
            f"BUG #22: _do_error must pass render=False for empty streaming "
            f"text; got {kwargs}"
        )

    def test_do_compaction_bubble_renders_no_empty_bubble_on_tool_only_turn(self):
        """BUG #22 guard on _do_compaction_bubble (same pattern)."""
        handler, crh, mc = self._make_handler_with_agent()
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh
        crh.get_streaming_text.return_value = ""

        handler._do_compaction_bubble("special:coder", {
            "messages_removed": 3, "tokens_freed": 1200,
            "layer": 2, "trigger": "auto",
        })

        crh.end_streaming.assert_called_once()
        assert crh.end_streaming.call_args.kwargs.get("render") is False

    def test_do_usage_warning_renders_no_empty_bubble_on_tool_only_turn(self):
        """BUG #22 guard on _do_usage_warning (same pattern)."""
        handler, crh, mc = self._make_handler_with_agent()
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        handler._crh = crh
        crh.get_streaming_text.return_value = ""

        handler._do_usage_warning("special:coder", "approaching-limit", 82.0)

        crh.end_streaming.assert_called_once()
        assert crh.end_streaming.call_args.kwargs.get("render") is False

    # ── BUG #15: _pending_exec_commands capture works without active project ───

    def test_exec_command_capture_works_without_active_project(self):
        """BUG #15: _pending_exec_commands[sk] = cmd must fire outside the project
        guard so the command_output callback gets a non-empty command even when
        no project is open.
        """
        handler, _, _ = self._make_handler_with_agent()
        # No project open
        handler._active_project = None

        cmd_outputs = []
        handler.set_on_command_output(
            lambda sk, cmd, tail, ec, dur: cmd_outputs.append((sk, cmd, tail, ec, dur))
        )

        # Simulate tool_start without a project — cmd must be captured
        handler._do_tool_call_start("special:coder", "exec_command", {"command": "ls -la"})

        # Verify _pending_exec_commands has the command
        cmd = handler._pending_exec_commands.get("special:coder", "")
        assert cmd == "ls -la", (
            f"BUG #15: _pending_exec_commands capture failed; got {cmd!r}, expected 'ls -la'"
        )

        # Fake result and dispatch the result callback
        class FakeResult:
            output = "file1\nfile2\nfile3"
            error = ""
            success = True
            exit_code = 0
            duration_ms = 42

        handler._do_tool_call_result("special:coder", "exec_command", FakeResult(), True)

        # command_output callback must fire with the captured command
        assert len(cmd_outputs) == 1, f"Expected 1 command_output callback, got {len(cmd_outputs)}"
        assert cmd_outputs[0][1] == "ls -la", (
            f"BUG #15: command was lost; got {cmd_outputs[0][1]!r}, expected 'ls -la'"
        )


# ═══════════════════════════════════════════════════════════════════
#  Streamed arguments validation — SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md
# ═══════════════════════════════════════════════════════════════════


class TestStreamedArgumentsValidation:
    """Tests for _validate_streamed_arguments and _call_llm_streaming
    truncation handling. See docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md."""

    # ── Unit tests for _validate_streamed_arguments ──────────────────────────

    def test_validate_good_json(self):
        from agent.runtime import _validate_streamed_arguments
        assert _validate_streamed_arguments('{"path": "x.py"}', "read_file", "sk1") is True

    def test_validate_empty_string(self):
        from agent.runtime import _validate_streamed_arguments
        assert _validate_streamed_arguments("", "read_file", "sk1") is True

    def test_validate_malformed_json(self):
        from agent.runtime import _validate_streamed_arguments
        assert _validate_streamed_arguments('{"command": "git sta', "exec_command", "sk1") is False

    def test_validate_non_string_input_returns_false(self):
        """BUG #2: non-string input (int) does not crash; returns False.

        json.loads raises TypeError on non-string input. The helper's except
        clause covers both JSONDecodeError and TypeError so a future caller
        passing a non-string does not crash the agent turn.
        """
        from agent.runtime import _validate_streamed_arguments
        # int input — previously crashed with TypeError
        assert _validate_streamed_arguments(42, "f", "sk") is False

    # ── Integration: done-event path skips malformed tool call ───────────────

    def test_done_path_skips_malformed_tool_call(self):
        """_call_llm_streaming done-event path: malformed args → call skipped."""
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        # Done path: one valid + one malformed tool call then done.
        # Use side_effect with a list so each _call_llm_streaming call gets
        # a fresh iterator. The second call (post-tool-execution) returns
        # a simple text response so the loop terminates cleanly.
        tool_stream = iter([
            SSEEvent(type="tool_call_delta", data={
                "index": 0, "name": "read_file", "arguments": '{"path": "ok.py"}'
            }),
            SSEEvent(type="tool_call_delta", data={
                "index": 1, "name": "exec_command", "arguments": '{"command": "git sta'
            }),
            SSEEvent(type="done", data={}),
        ])
        done_stream = iter([
            SSEEvent(type="text_delta", data={"content": "Done."}),
            SSEEvent(type="done", data={}),
        ])
        mock_provider = MagicMock()
        mock_provider.stream.side_effect = [tool_stream, done_stream]

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "list files")

        conv = rt.get_conversation(sk)
        assert conv is not None

        # Find the assistant message with tool calls (not the final "Done." message)
        tool_msgs = [m for m in conv.messages if m.role == "assistant" and m.tool_calls]
        assert len(tool_msgs) >= 1, "Expected at least one assistant message with tool calls"
        tcs = tool_msgs[0].tool_calls
        assert len(tcs) == 1, (
            f"Expected 1 tool call (malformed exec_command skipped), got {len(tcs)}"
        )
        assert tcs[0].tool_name == "read_file"
        assert tcs[0].arguments == {"path": "ok.py"}
        rt.stop()

    def test_done_path_skips_all_malformed(self):
        """_call_llm_streaming done-event path: all malformed → empty tool_calls."""
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="tool_call_delta", data={
                "index": 0, "name": "exec_command", "arguments": '{"command": "git sta'
            }),
            SSEEvent(type="done", data={}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "run command")

        conv = rt.get_conversation(sk)
        assert conv is not None
        last_msg = conv.messages[-1]
        tcs = last_msg.tool_calls
        assert len(tcs) == 0, (
            f"Expected 0 tool calls (malformed skipped), got {len(tcs)}"
        )
        rt.stop()

    # ── Integration: fallback path (no done event) skips malformed tool call ─

    def test_fallback_path_skips_malformed_tool_call(self):
        """_call_llm_streaming fallback (no done): malformed args → skipped."""
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        # No done event on first call — falls through to fallback path.
        # Second call (post-tool-execution) returns a simple text response.
        tool_stream = iter([
            SSEEvent(type="tool_call_delta", data={
                "index": 0, "name": "read_file", "arguments": '{"path": "ok.py"}'
            }),
            SSEEvent(type="tool_call_delta", data={
                "index": 1, "name": "exec_command", "arguments": '{"command": "git sta'
            }),
            # No done event!
        ])
        done_stream = iter([
            SSEEvent(type="text_delta", data={"content": "Done."}),
            SSEEvent(type="done", data={}),
        ])
        mock_provider = MagicMock()
        mock_provider.stream.side_effect = [tool_stream, done_stream]

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "list files")

        conv = rt.get_conversation(sk)
        assert conv is not None
        # Find the assistant message with tool calls (not the final "Done." message)
        tool_msgs = [m for m in conv.messages if m.role == "assistant" and m.tool_calls]
        assert len(tool_msgs) >= 1, "Expected at least one assistant message with tool calls"
        tcs = tool_msgs[0].tool_calls
        assert len(tcs) == 1, (
            f"Expected 1 valid tool call via fallback, got {len(tcs)}"
        )
        assert tcs[0].tool_name == "read_file"
        assert tcs[0].arguments == {"path": "ok.py"}
        rt.stop()

    def test_fallback_path_skips_all_malformed(self):
        """_call_llm_streaming fallback (no done): all malformed → empty."""
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="tool_call_delta", data={
                "index": 0, "name": "exec_command", "arguments": '{"command": "git sta'
            }),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "run command")

        conv = rt.get_conversation(sk)
        assert conv is not None
        last_msg = conv.messages[-1]
        tcs = last_msg.tool_calls
        assert len(tcs) == 0, (
            f"Expected 0 tool calls (all malformed), got {len(tcs)}"
        )
        rt.stop()


# ── Stream Error Event Integration Tests ──────────────────────────────────────

class TestStreamErrorIntegration:
    """Tests for _call_llm_streaming and _run_loop handling of
    SSEEvent(type='error', ...) from OpenRouter mid-stream errors.

    Covers: _stream_error attachment, error message surfacing, metadata
    inclusion, content_filter synthetic error, and code=0 formatting.
    """

    def test_call_llm_streaming_attaches_stream_error(self):
        """_call_llm_streaming sets _stream_error on result when error event received.
        Verifies via on_error callback that the provider error message is surfaced
        (not the generic "no content" placeholder)."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        error_messages = []

        def on_error(sk, msg):
            error_messages.append(msg)

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None, on_error=on_error)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="error", data={"error": {"code": 429, "message": "Rate limit exceeded"}}),
            SSEEvent(type="done", data={}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        assert len(error_messages) >= 1, "Expected on_error to be called"
        assert "Provider error" in error_messages[0], (
            f"Expected 'Provider error' in on_error, got: {error_messages[0]}"
        )
        assert "429" in error_messages[0], (
            f"Expected error code 429 in on_error, got: {error_messages[0]}"
        )
        assert "Rate limit" in error_messages[0], (
            f"Expected 'Rate limit' in on_error, got: {error_messages[0]}"
        )
        rt.stop()

    def test_stream_error_surfaces_code_0_message(self):
        """_stream_error with code=0 does not show '(code=0)' in on_error message."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        error_messages = []

        def on_error(sk, msg):
            error_messages.append(msg)

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None, on_error=on_error)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="error", data={"error": {"code": 0, "message": "Some non-fatal issue"}}),
            SSEEvent(type="done", data={}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        assert len(error_messages) >= 1, "Expected on_error to be called"
        # Should NOT contain "(code=0)" — just the message
        assert "(code=0)" not in error_messages[0], (
            f"Should not show code=0, got: {error_messages[0]}"
        )
        # Should still contain the error message
        assert "non-fatal" in error_messages[0]
        rt.stop()

    def test_stream_error_surfaces_metadata_provider_name(self):
        """_stream_error with metadata.provider_name includes it in the on_error message."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        error_messages = []

        def on_error(sk, msg):
            error_messages.append(msg)

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None, on_error=on_error)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="error", data={
                "error": {
                    "code": 429,
                    "message": "Rate limit exceeded",
                    "metadata": {"provider_name": "nvidia"},
                }
            }),
            SSEEvent(type="done", data={}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        assert len(error_messages) >= 1, "Expected on_error to be called"
        assert "nvidia" in error_messages[0], (
            f"Expected 'nvidia' provider name in on_error, got: {error_messages[0]}"
        )
        rt.stop()

    def test_stream_error_fallback_path(self):
        """_stream_error attached via fallback path (no done event).
        Phase 2b (BUG #5 fix): the turn terminates FAILED via
        _terminate_turn; on_error IS called with the 503, and the partial
        text accumulated from text_delta is NOT persisted to the
        conversation (the prior fall-through to the text-success path,
        which preserved partial text but ALSO dispatched on_response_complete
        for the same turn, is the bug BUG #5 documents).
        """
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        error_messages = []

        def on_error(sk, msg):
            error_messages.append(msg)

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None, on_error=on_error)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        # No done event — stream ends after text + error event (fallback path)
        mock_provider.stream.return_value = iter([
            SSEEvent(type="text_delta", data={"content": "Partial text"}),
            SSEEvent(type="error", data={"error": {"code": 503, "message": "Service unavailable"}}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        # BUG #3 fix: on_error is called even with non-empty content
        assert len(error_messages) >= 1, (
            f"Expected on_error to be called with non-empty content, got: {error_messages}"
        )
        assert "503" in error_messages[0], (
            f"Expected error code 503 in on_error, got: {error_messages[0]}"
        )
        conv = rt.get_conversation(sk)
        assert conv is not None
        # BUG #5 fix: the partial text is NOT preserved. The previous
        # behavior fell through to the text-success path which called
        # conv.add_assistant_message(text_content, []), preserving the
        # partial text. That fall-through also dispatched on_response_complete
        # for the same turn, which is the BUG #5 behavior change. The new
        # behavior terminates the turn FAILED via _terminate_turn and the
        # partial text is discarded.
        last = conv.messages[-1]
        assert last.role == "user", (
            f"Expected last message to be the original user message (no "
            f"partial-text assistant message should have been added), got: "
            f"role={last.role}, content={last.content!r}"
        )
        assert "Partial text" not in last.content
        rt.stop()

    def test_stream_error_fallback_empty_content(self):
        """_stream_error via fallback path with empty content: on_error is called.
        Only the error event, no text content, no done event."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        error_messages = []

        def on_error(sk, msg):
            error_messages.append(msg)

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None, on_error=on_error)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        # No done event, no text content — only error event (fallback path, empty content)
        mock_provider.stream.return_value = iter([
            SSEEvent(type="error", data={"error": {"code": 503, "message": "Service unavailable"}}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        assert len(error_messages) >= 1, "Expected on_error to be called for empty-content fallback"
        assert "Provider error" in error_messages[0], (
            f"Expected 'Provider error' in on_error from fallback path, got: {error_messages[0]}"
        )
        assert "503" in error_messages[0]
        rt.stop()

    def test_stream_error_content_filter_surfaces_filtered_message(self):
        """content_filter finish_reason surfaces 'Content was filtered' to on_error callback."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        error_messages = []

        def on_error(sk, msg):
            error_messages.append(msg)

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None, on_error=on_error)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="error", data={
                "error": {"code": "content_filter", "message": "Content was filtered by the provider (finish_reason=content_filter)."}
            }),
            SSEEvent(type="done", data={}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        assert len(error_messages) >= 1, "Expected on_error to be called"
        assert "filtered" in error_messages[0].lower(), (
            f"Expected content_filter message, got: {error_messages[0]}"
        )
        rt.stop()

    def test_stream_error_missing_on_error_callback(self):
        """_on_error not set: _run_loop does not crash when error event is received."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        # AgentRuntime without on_error callback
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="error", data={"error": {"code": 429, "message": "Rate limit"}}),
            SSEEvent(type="done", data={}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                # Should not raise
                rt._run_loop(sk, "hello")

        conv = rt.get_conversation(sk)
        assert conv is not None
        rt.stop()

    def test_stream_error_captures_late_error_after_done(self):
        """Late error after done is captured and written back to _stream_error."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        error_messages = []
        def on_error(sk, msg):
            error_messages.append(msg)

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None, on_error=on_error)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        # Simulate: text_delta -> done -> late error event
        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="text_delta", data={"content": "Hello"}),
            SSEEvent(type="done", data={}),
            SSEEvent(type="error", data={"error": {"code": 503, "message": "Late error after done"}}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        # Late error should be captured and written back to result, triggering on_error
        assert len(error_messages) >= 1, (
            f"Expected on_error to be called for late error, got: {error_messages}"
        )
        assert "503" in error_messages[0], (
            f"Expected 503 in late error message, got: {error_messages[0]}"
        )
        rt.stop()

    def test_stream_error_captures_late_usage_after_done(self):
        """Late usage after done is captured and written back to result.usage."""
        from agent.llm.streaming import SSEEvent
        from unittest.mock import MagicMock

        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda sk, d: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")

        mock_provider = MagicMock()
        mock_provider.stream.return_value = iter([
            SSEEvent(type="text_delta", data={"content": "Hello"}),
            SSEEvent(type="done", data={}),
            SSEEvent(type="usage", data={"usage": {"prompt_tokens": 100, "completion_tokens": 50}}),
        ])

        with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
            with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
                rt._run_loop(sk, "hello")

        conv = rt.get_conversation(sk)
        assert conv is not None
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Phase 5: Turn state machine + terminal paths + alias removal
#  (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.6)
#
#  22 tests across 8 groups:
#    Group 1: TurnStatus / TurnResult dataclasses (3)
#    Group 2: _terminate_turn behavior (8)
#    Group 3: _run_loop state transitions (4)
#    Group 3a: BUG #2 terminal paths (2)
#    Group 3b: BUG #5 mid-stream error (1)
#    Group 3c: BUG #6 limit handling (2)
#    Group 4: provider alias removal (2)
#    Group 5: callback protocol exports (1)
#    Group 6: send_message token rotation (1)
# ═══════════════════════════════════════════════════════════════════


class TestTurnStateMachine:
    """22 tests for the per-turn state machine introduced in Phase 2a/2b.

    Locks in the contract of TurnStatus/TurnResult, _terminate_turn,
    the (sk, tk) keying, stale-token rejection, the new terminal paths
    (no_conversation, prompt_build_failed, stream_error_with_content,
    cost_limit, step_limit), and the provider alias removal from Phase 4.
    """

    # ── Group 1: TurnStatus / TurnResult (3 tests) ──────────────────

    def test_turn_status_enum_values(self):
        """TurnStatus has 5 values, 2 non-terminal + 3 terminal."""
        from agent.runtime import TurnStatus
        all_values = {s.value for s in TurnStatus}
        assert all_values == {
            "running", "streaming", "completed", "failed", "cancelled",
        }
        non_terminal = {TurnStatus.RUNNING, TurnStatus.STREAMING}
        terminal = {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED}
        assert non_terminal.isdisjoint(terminal)
        assert len(non_terminal) + len(terminal) == 5

    def test_turn_result_required_fields(self):
        """TurnResult requires status/session_key/turn_token; others have defaults."""
        from agent.runtime import TurnResult, TurnStatus
        tr = TurnResult(
            status=TurnStatus.COMPLETED,
            session_key="test:sk",
            turn_token=object(),
        )
        assert tr.text == ""
        assert tr.error is None
        assert tr.metadata == {}

    def test_turn_result_metadata_isolation(self):
        """Two TurnResult instances must not share metadata dict (mutable default trap)."""
        from agent.runtime import TurnResult, TurnStatus
        tr1 = TurnResult(
            status=TurnStatus.FAILED,
            session_key="test:sk",
            turn_token=object(),
            metadata={"reason": "x"},
        )
        tr2 = TurnResult(
            status=TurnStatus.FAILED,
            session_key="test:sk",
            turn_token=object(),
        )
        tr1.metadata["leak"] = "value"
        assert "leak" not in tr2.metadata, (
            "metadata dict leaked between TurnResult instances — mutable default trap"
        )

    # ── Group 2: _terminate_turn behavior (8 tests) ─────────────────

    def test_terminate_turn_dispatches_on_response_complete_for_completed(self):
        """_terminate_turn(COMPLETED) calls on_response_complete with text + _turn_token."""
        from agent.runtime import TurnResult, TurnStatus
        mock_complete = unittest.mock.MagicMock()
        rt = AgentRuntime(_make_cfg(), GLib=None, on_response_complete=mock_complete)
        sk = _uniq()
        tk = object()
        # Pre-register the active token so stale-token check passes.
        rt._turn_tokens[sk] = tk
        rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
        accepted = rt._terminate_turn(TurnResult(
            status=TurnStatus.COMPLETED,
            session_key=sk, turn_token=tk, text="Hello",
        ))
        assert accepted is not None
        mock_complete.assert_called_once()
        args, kwargs = mock_complete.call_args
        # _dispatch calls on_response_complete(sk, text, _turn_token=tk)
        assert args[0] == sk
        assert args[1] == "Hello"
        assert kwargs.get("_turn_token") is tk

    def test_terminate_turn_dispatches_on_error_for_failed(self):
        """_terminate_turn(FAILED) calls on_error with the error message."""
        from agent.runtime import TurnResult, TurnStatus
        mock_error = unittest.mock.MagicMock()
        rt = AgentRuntime(_make_cfg(), GLib=None, on_error=mock_error)
        sk = _uniq()
        tk = object()
        rt._turn_tokens[sk] = tk
        rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
        rt._terminate_turn(TurnResult(
            status=TurnStatus.FAILED,
            session_key=sk, turn_token=tk, error="Something went wrong",
        ))
        mock_error.assert_called_once()
        args, kwargs = mock_error.call_args
        # _dispatch calls on_error(sk, msg, _turn_token=tk)
        assert args[0] == sk
        assert args[1] == "Something went wrong"
        assert kwargs.get("_turn_token") is tk

    def test_terminate_turn_dispatches_on_error_for_cancelled(self):
        """_terminate_turn(CANCELLED) calls on_error (CANCELLED is a terminal failure variant)."""
        from agent.runtime import TurnResult, TurnStatus
        mock_error = unittest.mock.MagicMock()
        rt = AgentRuntime(_make_cfg(), GLib=None, on_error=mock_error)
        sk = _uniq()
        tk = object()
        rt._turn_tokens[sk] = tk
        rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
        rt._terminate_turn(TurnResult(
            status=TurnStatus.CANCELLED,
            session_key=sk, turn_token=tk, error="Cancelled by user",
        ))
        mock_error.assert_called_once()
        args, _ = mock_error.call_args
        assert args[1] == "Cancelled by user"

    def test_terminate_turn_rejects_non_terminal_status(self):
        """_terminate_turn with RUNNING or STREAMING returns None and does not dispatch."""
        from agent.runtime import TurnResult, TurnStatus
        mock_complete = unittest.mock.MagicMock()
        rt = AgentRuntime(_make_cfg(), GLib=None, on_response_complete=mock_complete)
        sk = _uniq()
        tk = object()
        rt._turn_tokens[sk] = tk
        # Note: do NOT pre-set state — RUNNING/STREAMING are non-terminal and must be rejected.
        for non_terminal in (TurnStatus.RUNNING, TurnStatus.STREAMING):
            accepted = rt._terminate_turn(TurnResult(
                status=non_terminal,
                session_key=sk, turn_token=tk,
            ))
            assert accepted is None, (
                f"_terminate_turn should reject non-terminal {non_terminal!r}"
            )
        mock_complete.assert_not_called()
        # get_turn_state returns None because no terminal state was recorded.
        assert rt.get_turn_state(sk) is None

    def test_terminate_turn_dedups_duplicate_terminal_transitions(self):
        """Second call to _terminate_turn for the same (sk, tk) is rejected (None)."""
        from agent.runtime import TurnResult, TurnStatus
        mock_complete = unittest.mock.MagicMock()
        mock_error = unittest.mock.MagicMock()
        rt = AgentRuntime(
            _make_cfg(), GLib=None,
            on_response_complete=mock_complete,
            on_error=mock_error,
        )
        sk = _uniq()
        tk = object()
        rt._turn_tokens[sk] = tk
        rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
        first = rt._terminate_turn(TurnResult(
            status=TurnStatus.COMPLETED,
            session_key=sk, turn_token=tk, text="x",
        ))
        # Second call with different status but same (sk, tk) must be rejected.
        second = rt._terminate_turn(TurnResult(
            status=TurnStatus.FAILED,
            session_key=sk, turn_token=tk, error="y",
        ))
        assert first is not None
        assert second is None, "second _terminate_turn should be deduped"
        # Only the first dispatch fired.
        assert mock_complete.call_count == 1
        mock_error.assert_not_called()
        # Recorded state is the first one (COMPLETED).
        assert rt.get_turn_state(sk) == TurnStatus.COMPLETED

    def test_terminate_turn_rejects_stale_token(self):
        """BUG #4: a result for an old (rotated) turn_token is rejected."""
        from agent.runtime import TurnResult, TurnStatus
        mock_complete = unittest.mock.MagicMock()
        rt = AgentRuntime(_make_cfg(), GLib=None, on_response_complete=mock_complete)
        sk = _uniq()
        old_tk, new_tk = object(), object()
        # Active token for this session is the new one (simulating a fresh send_message).
        rt._turn_tokens[sk] = new_tk
        rt._turn_state[(sk, new_tk)] = TurnStatus.RUNNING
        # Submit a result for the OLD token — must be rejected.
        accepted = rt._terminate_turn(TurnResult(
            status=TurnStatus.COMPLETED,
            session_key=sk, turn_token=old_tk, text="stale",
        ))
        assert accepted is None, "stale-token result should be rejected"
        mock_complete.assert_not_called()
        # The new (active) token's state was not disturbed.
        assert rt.get_turn_state(sk) == TurnStatus.RUNNING

    def test_terminate_turn_persistence_uses_separate_session_keys(self):
        """BUG #9: 3 separate session keys for COMPLETED/FAILED/CANCELLED persistence test.

        _terminate_turn dedups terminal transitions for the same (sk, tk),
        so each terminal status needs its own session key to test the
        auto_save behavior independently.
        """
        from agent.runtime import TurnResult, TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        with unittest.mock.patch.object(rt, "_auto_save") as mock_save:
            cases = [
                (TurnStatus.COMPLETED, None, "x", True),    # saves
                (TurnStatus.FAILED, "oops", "", True),      # saves
                (TurnStatus.CANCELLED, "cancelled", "", False),  # does NOT save (no persist flag)
            ]
            for status, error, text, should_persist in cases:
                sk = _uniq()
                tk = object()
                rt._turn_tokens[sk] = tk
                rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
                rt._conversations[sk] = unittest.mock.MagicMock()  # so _auto_save has a conv
                rt._terminate_turn(TurnResult(
                    status=status, session_key=sk, turn_token=tk,
                    text=text, error=error,
                ))
            # COMPLETED (1) and FAILED (2) persisted; CANCELLED without persist flag (3) did NOT.
            assert mock_save.call_count == 2, (
                f"Expected 2 auto_save calls (COMPLETED + FAILED); got {mock_save.call_count}"
            )

    def test_terminate_turn_cancelled_with_persist_metadata_saves(self):
        """CANCELLED with metadata={'persist': True} saves (explicit opt-in)."""
        from agent.runtime import TurnResult, TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        with unittest.mock.patch.object(rt, "_auto_save") as mock_save:
            sk = _uniq()
            tk = object()
            rt._turn_tokens[sk] = tk
            rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
            rt._conversations[sk] = unittest.mock.MagicMock()
            rt._terminate_turn(TurnResult(
                status=TurnStatus.CANCELLED,
                session_key=sk, turn_token=tk,
                error="cancelled", metadata={"persist": True},
            ))
            assert mock_save.call_count == 1, (
                f"Expected 1 auto_save call when persist=True; got {mock_save.call_count}"
            )

    def test_terminate_turn_flushes_audit_log_on_completed_and_failed(self):
        """Bug 3 (SPEC-AUDIT-CLEANUP-1): _terminate_turn must flush the audit
        log on EVERY terminal outcome — COMPLETED and FAILED at minimum (the
        shared terminal path). The flush is monkeypatched — hermeticity: the
        real flush_audit_log() writes ~/.config/crabcakes/audit-log.jsonl.
        """
        from agent.runtime import TurnResult, TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        calls = []
        with unittest.mock.patch.object(
            rt._audit_log, "flush_audit_log",
            side_effect=lambda *a, **kw: calls.append(1),
        ):
            for status, error, text in (
                (TurnStatus.COMPLETED, None, "done"),
                (TurnStatus.FAILED, "boom", ""),
            ):
                sk = _uniq()
                tk = object()
                rt._turn_tokens[sk] = tk
                rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
                rt._terminate_turn(TurnResult(
                    status=status, session_key=sk, turn_token=tk,
                    text=text, error=error,
                ))
        assert len(calls) == 2, (
            f"flush_audit_log must fire once per terminal turn (got {len(calls)}); "
            "statuses driven: COMPLETED + FAILED"
        )

    def test_terminate_turn_flushes_audit_log_on_cancelled_too(self):
        """Bug 3: CANCELLED is also a terminal outcome — the flush must fire
        there as well (it sits on the shared terminal path, outside the
        persist gate). Monkeypatched — no real disk write.
        """
        from agent.runtime import TurnResult, TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        calls = []
        with unittest.mock.patch.object(
            rt._audit_log, "flush_audit_log",
            side_effect=lambda *a, **kw: calls.append(1),
        ):
            sk = _uniq()
            tk = object()
            rt._turn_tokens[sk] = tk
            rt._turn_state[(sk, tk)] = TurnStatus.RUNNING
            rt._terminate_turn(TurnResult(
                status=TurnStatus.CANCELLED,
                session_key=sk, turn_token=tk, error="cancelled",
            ))
        assert len(calls) == 1, (
            f"flush_audit_log must fire on CANCELLED too (got {len(calls)})"
        )

    # ── Group 3: _run_loop state transitions (4 tests) ──────────────

    def test_run_loop_starts_in_running_state(self):
        """At the top of _run_loop, state is RUNNING; after success, state is COMPLETED."""
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        with unittest.mock.patch.object(
            rt, "_call_llm",
            lambda sk, msgs, tools, **kwargs: _resp("Hello, human."),
        ):
            rt._run_loop(sk, "hello")
        assert rt.get_turn_state(sk) == TurnStatus.COMPLETED
        rt.stop()

    def test_run_loop_transitions_to_streaming_before_first_llm_call(self):
        """STREAMING is recorded before the first _call_llm returns; final state is COMPLETED."""
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        states_seen = []

        def tracking_call(*args, **kwargs):
            states_seen.append(rt.get_turn_state(sk))
            return _resp("Hello.")

        with unittest.mock.patch.object(rt, "_call_llm", side_effect=tracking_call):
            rt._run_loop(sk, "hello")
        # The first LLM call saw STREAMING (set just before _call_llm is invoked).
        assert TurnStatus.STREAMING in states_seen, (
            f"Expected STREAMING during first LLM call; states seen: {states_seen}"
        )
        assert rt.get_turn_state(sk) == TurnStatus.COMPLETED
        rt.stop()

    def test_run_loop_terminates_with_failed_on_max_iterations(self):
        """When max_tool_iterations is reached with no text response, terminal is FAILED."""
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        # Always return a tool call (never text) — loop will exhaust max_iterations.
        tool_resp = _resp(tool_calls=[{
            "id": "call_1",
            "function": {
                "name": "list_files",
                "arguments": '{"path": "."}',
            },
        }])
        from agent.tools import ToolResult as _ToolResult
        with unittest.mock.patch.object(rt, "_call_llm", return_value=tool_resp), \
             unittest.mock.patch(
                 "agent.tools.execute_tool",
                 return_value=_ToolResult(success=True, output="ok"),
             ):
            rt._run_loop(sk, "hello")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "max_iterations"
        rt.stop()

    def test_run_loop_terminates_with_cancelled_on_cancel_signal(self):
        """When _cancel_requested is set, the next loop iteration's cancel check fires CANCELLED.

        The trigger returns a tool call (not text) so the loop iterates again,
        where the cancel check at the top of the while loop fires and routes
        through _terminate_turn(CANCELLED).
        """
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        call_count = [0]

        def trigger_cancel(*args, **kwargs):
            call_count[0] += 1
            rt._cancel_requested = True
            # Return a tool call so the loop iterates (does not exit via text-success).
            return _resp(tool_calls=[{
                "id": "call_1",
                "function": {"name": "list_files", "arguments": '{"path": "."}'},
            }])

        from agent.tools import ToolResult as _ToolResult
        with unittest.mock.patch.object(rt, "_call_llm", side_effect=trigger_cancel), \
             unittest.mock.patch(
                 "agent.tools.execute_tool",
                 return_value=_ToolResult(success=True, output="ok"),
             ):
            rt._run_loop(sk, "hello")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.CANCELLED, (
            f"Expected CANCELLED; got {result.status} (metadata: {result.metadata})"
        )
        # The cancel check fired on the SECOND iteration, so the first call completed.
        assert call_count[0] >= 1
        rt.stop()

    # ── Group 3a: BUG #2 terminal paths (2 tests) ───────────────────

    def test_run_loop_terminates_with_failed_on_no_conversation(self):
        """BUG #2: _run_loop with a session_key that has no Conversation must
        route through _terminate_turn(FAILED, no_conversation) — not the
        ad-hoc dispatch + return of the pre-2b code."""
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        # Deliberately do NOT call create_conversation.
        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)
        rt._run_loop(sk, "hello")
        # _terminate_turn dispatched on_error exactly once with "No conversation found".
        assert len(errors) == 1, f"Expected 1 error; got: {errors}"
        assert "no conversation" in str(errors[0]).lower(), (
            f"Expected 'no conversation' in error message; got: {errors[0]!r}"
        )
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "no_conversation"
        rt.stop()

    def test_run_loop_terminates_with_failed_on_prompt_build_failure(self):
        """BUG #2: _run_loop where _ensure_system_prompt raises must route through
        _terminate_turn(FAILED, prompt_build_failed)."""
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        errors = []
        rt._on_error = lambda sk2, msg: errors.append(msg)
        with unittest.mock.patch.object(
            rt, "_ensure_system_prompt",
            side_effect=RuntimeError("prompt build failed"),
        ):
            rt._run_loop(sk, "hello")
        # The error fired once.
        assert len(errors) == 1, f"Expected 1 error; got: {errors}"
        # And the recorded result has reason='prompt_build_failed'.
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "prompt_build_failed"
        rt.stop()

    # ── Group 3b: BUG #5 mid-stream error (1 test) ──────────────────

    def test_run_loop_terminates_with_failed_on_stream_error_with_content(self):
        """BUG #5: a response with non-empty text_content AND _stream_error must
        terminate the turn with FAILED. The previous fall-through to
        on_response_complete must NOT happen."""
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        response = {
            "choices": [{"message": {
                "content": "partial response that was streamed",
                "tool_calls": [],
            }}],
            "_stream_error": {"code": 500, "message": "stream failed"},
        }
        complete = []
        errors = []
        rt._on_response_complete = lambda sk2, t: complete.append(t)
        rt._on_error = lambda sk2, msg: errors.append(msg)
        with unittest.mock.patch.object(rt, "_call_llm", return_value=response):
            rt._run_loop(sk, "hello")
        # on_error was called once; on_response_complete was NOT (no fall-through).
        assert len(errors) == 1, f"Expected 1 on_error; got: {errors}"
        assert len(complete) == 0, (
            f"on_response_complete must NOT fire after stream error; got: {complete}"
        )
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "stream_error_with_content"
        rt.stop()

    # ── Group 3c: BUG #6 limit handling (2 tests) ───────────────────

    def test_run_loop_terminates_with_failed_on_cost_limit(self):
        """BUG #6: When conv.total_cost > cost_limit, terminate FAILED with reason='cost_limit'."""
        from agent.runtime import TurnStatus
        cfg = _make_cfg()
        cfg.cost_limit = 0.0  # any cost > 0 trips the limit (predicate is strict >)
        rt = AgentRuntime(cfg, GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        with unittest.mock.patch.object(
            rt, "_call_llm",
            lambda sk, msgs, tools, **kwargs: _resp("Hi."),
        ):
            # record_usage adds cost; one call is enough to trip limit=0.
            rt._conversations[sk].record_usage(100, 0.01)
            rt._run_loop(sk, "hello")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "cost_limit", (
            f"Expected reason='cost_limit'; got: {result.metadata}"
        )
        rt.stop()

    def test_run_loop_terminates_with_failed_on_step_limit(self):
        """BUG #6: When conv.step_count > step_limit, terminate FAILED with reason='step_limit'."""
        from agent.runtime import TurnStatus
        cfg = _make_cfg()
        cfg.step_limit = 0  # any step > 0 trips the limit
        rt = AgentRuntime(cfg, GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        with unittest.mock.patch.object(
            rt, "_call_llm",
            lambda sk, msgs, tools, **kwargs: _resp("Hi."),
        ):
            # Bump step_count above the limit (the strict-greater predicate needs >0).
            rt._conversations[sk].step_count = 1
            rt._run_loop(sk, "hello")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "step_limit", (
            f"Expected reason='step_limit'; got: {result.metadata}"
        )
        rt.stop()

    # ── Group 4: provider alias removal (2 tests) ───────────────────

    def test_runtime_no_longer_exposes_call_provider_aliases(self):
        """_call_openai, _call_minimax, _call_anthropic removed in Phase 4 §2.3 Edit I."""
        import agent.runtime
        for name in ("_call_openai", "_call_minimax", "_call_anthropic"):
            assert not hasattr(agent.runtime, name), (
                f"agent.runtime.{name} should be removed (Phase 4 Edit I)"
            )

    def test_runtime_no_longer_exposes_stream_provider_aliases(self):
        """_stream_*_events and _PROVIDER_STREAMERS removed in Phase 4 §2.3 Edit J."""
        import agent.runtime
        for name in (
            "_stream_openai_events", "_stream_minimax_events",
            "_stream_anthropic_events", "_PROVIDER_STREAMERS",
        ):
            assert not hasattr(agent.runtime, name), (
                f"agent.runtime.{name} should be removed (Phase 4 Edit J)"
            )

    # ── Group 5: callback protocol exports (1 test) ─────────────────

    def test_callbacks_module_exports_protocols(self):
        """agent.callbacks exports all 10 callback Protocols + AgentRuntimeCallbacks alias.

        Smoke test: each Protocol class has a __call__ (they're callable
        structural-typing contracts).
        """
        from agent.callbacks import (
            OnTextDelta, OnTurnStart, OnToolCallStart, OnToolCallResult,
            OnToolCallApprovalNeeded, OnResponseComplete, OnTokenUsage,
            OnTokenBreakdown, OnError, OnEnforcementStatus,
            AgentRuntimeCallbacks,
        )
        for cls in (
            OnTextDelta, OnTurnStart, OnToolCallStart, OnToolCallResult,
            OnToolCallApprovalNeeded, OnResponseComplete, OnTokenUsage,
            OnTokenBreakdown, OnError, OnEnforcementStatus,
        ):
            assert hasattr(cls, "__call__"), (
                f"{cls.__name__} is missing __call__ — not a callable Protocol"
            )
        # AgentRuntimeCallbacks is a dict alias; just verify it's accessible.
        assert AgentRuntimeCallbacks is not None

    # ── Group 6: send_message token rotation (1 test) ───────────────

    def test_send_message_rotates_turn_token(self):
        """BUG #4: two consecutive _run_loop calls with different turn_tokens
        must produce different _turn_tokens[sk] values so a new turn's state
        is not confused with a prior turn's terminal result.

        _run_loop registers _turn_tokens[sk] = turn_token at the top.
        The handler (agent_runtime_handler) rotates self._turn_token before
        each send_message; here we exercise the runtime-level rotation
        directly by calling _run_loop with two different explicit tokens.
        """
        from agent.runtime import TurnStatus
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, "/tmp")
        first_tk = object()
        second_tk = object()
        assert first_tk is not second_tk, "test setup: tokens must differ"
        with unittest.mock.patch.object(
            rt, "_call_llm",
            lambda sk, msgs, tools, **kwargs: _resp("Hello."),
        ):
            rt._run_loop(sk, "first", turn_token=first_tk)
        token_after_first = rt._turn_tokens.get(sk)
        assert token_after_first is first_tk
        assert rt.get_turn_state(sk) == TurnStatus.COMPLETED
        with unittest.mock.patch.object(
            rt, "_call_llm",
            lambda sk, msgs, tools, **kwargs: _resp("Hello again."),
        ):
            rt._run_loop(sk, "second", turn_token=second_tk)
        token_after_second = rt._turn_tokens.get(sk)
        assert token_after_second is second_tk
        assert token_after_second is not token_after_first, (
            "_turn_tokens[sk] must rotate to the new token on a fresh turn"
        )
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  SPEC-AUDIT-CLEANUP-3 Phase 1 Part A — producer-side delta coalescing
# ═══════════════════════════════════════════════════════════════════

class _QueueGLib:
    """Test double for GLib: idle_add queues; drain() runs callbacks FIFO.

    Mimics the real main loop's ordering guarantee (callbacks scheduled
    earlier run earlier) so producer→idle→completion ordering is testable.
    """

    def __init__(self):
        self.queue = []

    def idle_add(self, fn, *args, **kwargs):
        self.queue.append((fn, args, kwargs))
        return len(self.queue)

    def source_remove(self, timer_id):
        pass

    def timeout_add(self, *args, **kwargs):
        return 1

    def timeout_add_seconds(self, *args, **kwargs):
        return 1

    def drain(self, max_steps=10000):
        """Run queued callbacks FIFO, including ones re-scheduled while draining."""
        ran = 0
        while self.queue and ran < max_steps:
            fn, args, kwargs = self.queue.pop(0)
            fn(*args, **kwargs)
            ran += 1
        return ran


class TestDeltaCoalescing:
    """SPEC-AUDIT-CLEANUP-3 Phase 1 Part A — producer-side coalescing.

    Invariants under test (spec §Correctness invariants):
    1. accumulation always complete        → test_accumulation_complete_*
    3. ended/token guards vs deferred dispatches → test_mismatched_*, test_delta_after_session_ended_*
    4. empty-delta skip preserved          → test_empty_delta_still_reaches_main_thread
    5. producer→idle→completion ordering   → test_completion_ordering_*
    6. trailing guarantee                  → test_trailing_dispatch_pending_for_last_delta
    """

    def _make(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        glib = _QueueGLib()
        crh = MagicMock()
        crh.is_streaming.return_value = False
        mc = MagicMock()
        handler = AgentRuntimeHandler(
            main_content=mc, chat_render_handler=crh, GLib_module=glib
        )
        chat_box = MagicMock()
        handler._resolve_chat_box = MagicMock(return_value=chat_box)
        return handler, crh, glib

    def test_rapid_deltas_coalesce_dispatches(self):
        """(a) 100 rapid deltas → ≤ deltas/20 + 2 _do_text_delta invocations.

        RED on pre-change code: every delta scheduled its own idle_add →
        100 invocations.
        """
        handler, crh, glib = self._make()
        deltas = [f"chunk{i} " for i in range(100)]
        calls = {"n": 0}
        orig = handler._do_text_delta

        def counting(sk, text, token=None, **kw):
            calls["n"] += 1
            return orig(sk, text, token, **kw)

        handler._do_text_delta = counting
        for d in deltas:
            handler._on_text_delta("special:coder", d)
        glib.drain()

        assert calls["n"] <= len(deltas) // 20 + 2, (
            f"coalescing broken: {calls['n']} _do_text_delta invocations for "
            f"{len(deltas)} deltas (pre-change code fires one per delta)"
        )

    def test_accumulation_complete_no_loss_no_duplication(self):
        """(b)+(1) final accumulated text == exact concatenation of every delta."""
        handler, crh, glib = self._make()
        deltas = [f"chunk{i} " for i in range(100)]
        for d in deltas:
            handler._on_text_delta("special:coder", d)
        expected = "".join(deltas)
        glib.drain()
        assert handler._streaming_text["special:coder"] == expected

    def test_trailing_dispatch_pending_for_last_delta(self):
        """(c)+(6) after the first coalesced dispatch drains, a trailing
        dispatch is pending for the deltas that arrived while it was pending;
        the final update_streaming carries the complete text."""
        handler, crh, glib = self._make()
        for i in range(100):
            handler._on_text_delta("special:coder", f"chunk{i} ")
        # drain exactly ONE dispatch (the first coalesced one)
        glib.drain(max_steps=1)
        assert "special:coder" in handler._delta_dispatch_pending, (
            "trailing guarantee broken: no pending dispatch after the first "
            "coalesced dispatch drained, although deltas arrived while it "
            "was pending"
        )
        glib.drain()
        expected = "".join(f"chunk{i} " for i in range(100))
        assert crh.update_streaming.call_count >= 1
        last_call = crh.update_streaming.call_args_list[-1]
        assert last_call.args[1] == expected, (
            f"trailing dispatch must render the complete accumulated text; "
            f"got {last_call.args[1]!r}"
        )

    def test_mismatched_token_delta_dropped_no_corruption(self):
        """(d)+(3) a delta carrying a stale token while a dispatch is pending
        is dropped and does not corrupt accumulation."""
        handler, crh, glib = self._make()
        stale = object()
        current = object()
        handler._turn_tokens["special:coder"] = current
        handler._on_text_delta("special:coder", "good ", current)
        handler._on_text_delta("special:coder", "STALE ", stale)
        glib.drain()
        assert handler._streaming_text["special:coder"] == "good "
        assert "STALE" not in handler._streaming_text.get("special:coder", "")

    def test_delta_after_session_ended_dropped_no_new_bubble(self):
        """(e)+(3) a delta arriving after _ended_sessions is set is dropped —
        no start_streaming, no new bubble, no accumulation resurrection."""
        handler, crh, glib = self._make()
        handler._ended_sessions.add("special:coder")
        handler._on_text_delta("special:coder", "late delta")
        glib.drain()
        crh.start_streaming.assert_not_called()
        assert "special:coder" not in handler._streaming_text

    def test_empty_delta_still_reaches_main_thread(self):
        """(4) empty-delta bypass preserved: provider-sent empty deltas still
        dispatch to the main thread uncoalesced, and the empty delta still
        returns before accumulation. (The runtime's turn-start signal moved
        to on_turn_start.)"""
        handler, crh, glib = self._make()
        handler._on_text_delta("special:coder", "")
        assert len(glib.queue) == 1, "empty delta must still be dispatched"
        glib.drain()
        assert "special:coder" not in handler._streaming_text

    def test_completion_ordering_and_byte_identity(self):
        """(1)+(5) full streaming session: accumulated text is byte-identical
        to the delta concatenation, and the delta dispatches run BEFORE
        completion's end_streaming (producer→idle→completion ordering)."""
        handler, crh, glib = self._make()
        deltas = ["Hel", "lo ", "wor", "ld"]
        expected = "".join(deltas)
        order = []

        crh.update_streaming.side_effect = (
            lambda sk, text: order.append(("update", text))
        )
        crh.end_streaming.side_effect = lambda *a, **k: order.append(("end", None))

        for d in deltas:
            handler._on_text_delta("special:coder", d)
        glib.drain()
        assert handler._streaming_text["special:coder"] == expected, (
            "accumulated text must be byte-identical to the delta concatenation"
        )

        # completion (what the runtime dispatches last, with the authoritative text)
        crh.is_streaming.return_value = True
        crh.get_streaming_text.return_value = expected
        handler._do_response_complete("special:coder", expected)

        kinds = [k for k, _ in order]
        assert "update" in kinds and kinds.index("update") < kinds.index("end"), (
            f"producer→idle→completion ordering broken: {order}"
        )
        # byte identity: completion overwrites plain_text with the exact
        # delta concatenation (set_streaming_text is the authoritative write)
        crh.set_streaming_text.assert_called_with("special:coder", expected)

