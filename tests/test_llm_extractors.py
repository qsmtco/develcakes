"""Tests for agent/llm/extractors.py — response extraction functions.

Extracted from agent/runtime.py (Phase B3). Tests the public names
(no underscore). The response_format is passed explicitly — no mocking
of _RESPONSE_FORMAT needed.
"""

from agent.llm.extractors import (
    extract_tool_calls,
    extract_text_content,
    extract_usage,
)


# ======================================================================
# extract_tool_calls (spec §B.9 cases 23-25)
# ======================================================================


class TestExtractToolCalls:

    def test_extract_tool_calls_openai_format(self):
        """OpenAI-format response: choices[0].message.tool_calls parsed."""
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "call_1", "function": {"name": "write_file",
                 "arguments": '{"path": "x.py", "content": "hi"}'}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert len(calls) == 1
        assert calls[0][0] == "call_1"
        assert calls[0][1] == "write_file"
        assert calls[0][2] == {"path": "x.py", "content": "hi"}

    def test_extract_tool_calls_anthropic_format(self):
        """Anthropic-format response: content blocks with tool_use parsed."""
        response = {
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                 "input": {"path": "y.py"}},
            ],
        }
        calls = extract_tool_calls(response, response_format="anthropic")
        assert len(calls) == 1
        assert calls[0][0] == "toolu_1"
        assert calls[0][1] == "read_file"
        assert calls[0][2] == {"path": "y.py"}

    def test_extract_tool_calls_empty_choices(self):
        """No choices in OpenAI response → empty list."""
        assert extract_tool_calls({}, response_format="openai") == []
        assert extract_tool_calls({"choices": []}, response_format="openai") == []

    def test_extract_tool_calls_synthetic_id_for_empty_id(self):
        """Empty/None id → synthetic call_XXXXXX id (QTR-FIX)."""
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "", "function": {"name": "ls", "arguments": "{}"}},
                {"id": None, "function": {"name": "pwd", "arguments": "{}"}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert len(calls) == 2
        assert calls[0][0].startswith("call_")
        assert calls[1][0].startswith("call_")

    def test_extract_tool_calls_malformed_json_args_kept(self):
        """Malformed JSON is kept as an error marker, not dropped or raised.

        A skipped call used to vanish. The turn then looked successful and
        the model issued the same call again.
        """
        from agent.llm.extractors import is_truncated_arguments
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "c1", "function": {"name": "x", "arguments": "not-json"}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert len(calls) == 1
        assert calls[0][0] == "c1"
        assert calls[0][1] == "x"
        assert is_truncated_arguments(calls[0][2])
        assert calls[0][2]["__truncated_arguments__"] == "not-json"

    def test_extract_tool_calls_mixed_valid_and_malformed_args(self):
        """One valid + one malformed tool call → both returned."""
        from agent.llm.extractors import is_truncated_arguments
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "good", "function": {"name": "read_file",
                 "arguments": '{"path": "ok.py"}'}},
                {"id": "bad", "function": {"name": "exec_command",
                 "arguments": '{"command": "git sta'}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert len(calls) == 2
        assert calls[0][0] == "good"
        assert calls[0][1] == "read_file"
        assert calls[0][2] == {"path": "ok.py"}
        assert calls[1][0] == "bad"
        assert is_truncated_arguments(calls[1][2])

    def test_extract_tool_calls_empty_string_arguments_defaults_to_empty_dict(self):
        """BUG #1: empty-but-present arguments string defaults to {} (not dropped).

        Regression: a name-only tool call (zero args, common for Anthropic/MCP
        tools) used to be silently dropped because func.get("arguments", "{}")
        only defaults when the key is missing, not when it's an empty string.
        The streaming code always populates 'arguments' with '' (agent/runtime.py:1637),
        so the default never fired. Fix: func.get("arguments") or '{}'.
        See docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md (Phase 3, BUG #1).
        """
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "c0", "function": {"name": "clear_cache", "arguments": ""}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert len(calls) == 1
        assert calls[0][0] == "c0"
        assert calls[0][1] == "clear_cache"
        assert calls[0][2] == {}


# ======================================================================
# extract_text_content (spec §B.9 cases 26-27)
# ======================================================================


class TestExtractTextContent:

    def test_extract_text_content_openai(self):
        """OpenAI-format: choices[0].message.content returned."""
        response = {"choices": [{"message": {"content": "Hello world"}}]}
        assert extract_text_content(response, response_format="openai") == "Hello world"

    def test_extract_text_content_anthropic(self):
        """Anthropic-format: text content blocks joined into a string."""
        response = {"content": [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]}
        assert extract_text_content(response, response_format="anthropic") == "Hello world"

    def test_extract_text_content_empty_choices(self):
        """No choices → empty string."""
        assert extract_text_content({}, response_format="openai") == ""
        assert extract_text_content({"choices": []}, response_format="openai") == ""


# ======================================================================
# extract_usage (spec §B.9 cases 28-30)
# ======================================================================


class TestExtractUsage:

    def test_extract_usage_openai(self):
        """OpenAI: prompt_tokens/completion_tokens."""
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        assert extract_usage(response, response_format="openai") == (100, 50)

    def test_extract_usage_anthropic(self):
        """Anthropic: input_tokens/output_tokens."""
        response = {"usage": {"input_tokens": 200, "output_tokens": 80}}
        assert extract_usage(response, response_format="anthropic") == (200, 80)

    def test_extract_usage_missing(self):
        """No usage key → (0, 0)."""
        assert extract_usage({}, response_format="openai") == (0, 0)
        assert extract_usage({"usage": None}, response_format="openai") == (0, 0)


# ======================================================================
# Backward-compat re-exports
# ======================================================================


class TestReExports:
    # Re-export tests removed in Phase 8 — the underscored aliases no longer
    # exist in agent.runtime. The canonical functions live in agent.llm.extractors.
    pass
