# tests/test_conversation.py
# Unit tests for models/conversation.py
#
# Tests the contract — each public method's documented behavior.
# Edge cases that could break callers (AgentRuntime, context.py) are covered.

import pytest
from models.conversation import (
    Conversation,
    Message,
    ToolCall,
    MessageRole,
    ToolCallStatus,
    serialize_messages,
)


# ═══════════════════════════════════════════════════════════════════
#  ToolCall dataclass
# ═══════════════════════════════════════════════════════════════════

class TestToolCallDefaults:
    def test_default_status_is_pending(self):
        tc = ToolCall(call_id="call_1", tool_name="read_file", arguments={"path": "a.py"})
        assert tc.status == ToolCallStatus.PENDING
        assert tc.result is None
        assert tc.started_at is None
        assert tc.completed_at is None

    def test_mark_executing_sets_status_and_time(self):
        tc = ToolCall(call_id="call_1", tool_name="read_file", arguments={})
        tc.mark_executing()
        assert tc.status == ToolCallStatus.EXECUTING
        assert tc.started_at is not None

    def test_mark_completed_sets_result_and_time(self):
        tc = ToolCall(call_id="call_1", tool_name="read_file", arguments={})
        tc.mark_completed("file contents")
        assert tc.status == ToolCallStatus.COMPLETED
        assert tc.result == "file contents"
        assert tc.completed_at is not None

    def test_mark_failed_sets_error(self):
        tc = ToolCall(call_id="call_1", tool_name="read_file", arguments={})
        tc.mark_failed("Permission denied")
        assert tc.status == ToolCallStatus.FAILED
        assert tc.result == "Permission denied"
        assert tc.completed_at is not None


# ═══════════════════════════════════════════════════════════════════
#  Message dataclass
# ═══════════════════════════════════════════════════════════════════

class TestMessageDefaults:
    def test_default_timestamp_is_set(self):
        msg = Message(role=MessageRole.USER, content="hello")
        assert msg.timestamp is not None

    def test_default_tokens_used_is_zero(self):
        msg = Message(role=MessageRole.USER, content="hello")
        assert msg.tokens_used == 0

    def test_default_tool_calls_is_empty_list(self):
        msg = Message(role=MessageRole.USER, content="hello")
        assert msg.tool_calls == []
        assert isinstance(msg.tool_calls, list)

    def test_is_tool_call_true_for_assistant_with_tools(self):
        tc = ToolCall(call_id="c1", tool_name="read", arguments={})
        msg = Message(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
        assert msg.is_tool_call is True

    def test_is_tool_call_false_for_assistant_without_tools(self):
        msg = Message(role=MessageRole.ASSISTANT, content="hello", tool_calls=[])
        assert msg.is_tool_call is False

    def test_is_tool_result_true_for_tool_result_role(self):
        msg = Message(role=MessageRole.TOOL_RESULT, content="result", tool_call_id="c1")
        assert msg.is_tool_result is True

    def test_is_tool_result_false_for_other_roles(self):
        msg = Message(role=MessageRole.USER, content="hello")
        assert msg.is_tool_result is False


# ═══════════════════════════════════════════════════════════════════
#  Conversation dataclass
# ═══════════════════════════════════════════════════════════════════

class TestConversationDefaults:
    def test_project_path_defaults_to_none(self):
        c = Conversation(agent_name="Coder")
        assert c.project_path is None

    def test_system_prompt_defaults_to_empty(self):
        c = Conversation(agent_name="Coder")
        assert c.system_prompt == ""

    def test_messages_defaults_to_empty_list(self):
        c = Conversation(agent_name="Coder")
        assert c.messages == []
        assert isinstance(c.messages, list)

    def test_model_defaults_to_empty_string(self):
        c = Conversation(agent_name="Coder")
        assert c.model == ""

    def test_total_cost_defaults_to_zero(self):
        c = Conversation(agent_name="Coder")
        assert c.total_cost == 0.0

    def test_step_count_defaults_to_zero(self):
        c = Conversation(agent_name="Coder")
        assert c.step_count == 0

    def test_created_at_is_set(self):
        c = Conversation(agent_name="Coder")
        assert c.created_at is not None


class TestConversationMessageHelpers:
    def test_add_user_message_returns_message(self):
        c = Conversation(agent_name="Coder")
        msg = c.add_user_message("hello")
        assert msg.role == MessageRole.USER
        assert msg.content == "hello"
        assert len(c.messages) == 1

    def test_add_assistant_message_returns_message(self):
        c = Conversation(agent_name="Coder")
        msg = c.add_assistant_message("thinking...", [])
        assert msg.role == MessageRole.ASSISTANT
        assert msg.content == "thinking..."
        assert len(c.messages) == 1

    def test_add_assistant_message_increments_step_count(self):
        c = Conversation(agent_name="Coder")
        assert c.step_count == 0
        c.add_assistant_message("turn 1", [])
        assert c.step_count == 1
        c.add_assistant_message("turn 2", [])
        assert c.step_count == 2

    def test_add_assistant_message_with_tool_calls(self):
        c = Conversation(agent_name="Coder")
        tc = ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "a.py"})
        msg = c.add_assistant_message("", [tc])
        assert msg.tool_calls == [tc]
        assert msg.is_tool_call is True

    def test_add_tool_result_returns_message(self):
        c = Conversation(agent_name="Coder")
        msg = c.add_tool_result("c1", "file contents here")
        assert msg.role == MessageRole.TOOL_RESULT
        assert msg.content == "file contents here"
        assert msg.tool_call_id == "c1"

    def test_tool_result_does_not_increment_step_count(self):
        c = Conversation(agent_name="Coder")
        c.add_assistant_message("I will read the file", [])
        assert c.step_count == 1
        c.add_tool_result("c1", "file contents")
        assert c.step_count == 1  # still 1


class TestConversationToApiMessages:
    def test_empty_conversation_returns_nothing(self):
        c = Conversation(agent_name="Coder")
        assert c.to_api_messages() == []

    def test_system_prompt_becomes_first_system_message(self):
        c = Conversation(agent_name="Coder", system_prompt="You are helpful.")
        msgs = c.to_api_messages()
        assert msgs[0] == {"role": "system", "content": "You are helpful."}

    def test_user_message_format(self):
        c = Conversation(agent_name="Coder")
        c.add_user_message("hello world")
        msgs = c.to_api_messages()
        assert msgs[0] == {"role": "user", "content": "hello world"}

    def test_assistant_message_with_text(self):
        c = Conversation(agent_name="Coder")
        c.add_assistant_message("Here is the answer", [])
        msgs = c.to_api_messages()
        assert msgs[0] == {"role": "assistant", "content": "Here is the answer"}

    def test_assistant_message_with_tool_calls(self):
        c = Conversation(agent_name="Coder")
        tc = ToolCall(call_id="call_abc", tool_name="read_file", arguments={"path": "a.py"})
        c.add_assistant_message("", [tc])
        msgs = c.to_api_messages()
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["tool_calls"][0]["id"] == "call_abc"
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "read_file"
        assert msgs[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py"}'

    def test_tool_result_format(self):
        c = Conversation(agent_name="Coder")
        tc = ToolCall(call_id="call_abc", tool_name="read_file", arguments={"path": "a.py"})
        c.add_assistant_message("", [tc])
        c.add_tool_result("call_abc", "file contents")
        msgs = c.to_api_messages()
        assert msgs[1] == {
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": "file contents",
        }

    def test_full_conversation_sequence(self):
        c = Conversation(agent_name="Coder", system_prompt="You are Coder.")
        c.add_user_message("Implement auth")
        tc = ToolCall(call_id="c1", tool_name="write_file", arguments={"path": "auth.py", "content": ""})
        c.add_assistant_message("", [tc])
        c.add_tool_result("c1", "OK — wrote 100 bytes")
        c.add_assistant_message("Done.", [])
        msgs = c.to_api_messages()
        assert len(msgs) == 5  # system, user, assistant+tool, tool-result, assistant
        assert msgs[0] == {"role": "system", "content": "You are Coder."}
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"
        assert msgs[2]["tool_calls"][0]["function"]["name"] == "write_file"
        assert msgs[3]["role"] == "tool"
        assert msgs[4]["role"] == "assistant"

    def test_tool_result_follows_its_call_when_storage_interleaves(self, caplog):
        """D1: a nudge stored between a call and its result must not stay there.

        The unmatched-result half is the sad path: no dangling tool row.
        """
        import logging

        call = ToolCall(call_id="X", tool_name="read_file", arguments={})
        stored = [
            Message(role=MessageRole.ASSISTANT, content="", tool_calls=[call]),
            Message(role=MessageRole.USER, content="nudge"),
            Message(role=MessageRole.TOOL_RESULT, content="r", tool_call_id="X"),
        ]
        assert serialize_messages(stored) == [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "X",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "X", "content": "r"},
            {"role": "user", "content": "nudge"},
        ]

        with caplog.at_level(logging.WARNING, logger="models.conversation"):
            orphan = serialize_messages([
                Message(
                    role=MessageRole.TOOL_RESULT,
                    content="payload",
                    tool_call_id="missing",
                ),
            ])
        assert orphan == [
            {"role": "user", "content": "[tool result missing] — payload"},
        ]
        assert all(row["role"] != "tool" for row in orphan)
        assert any(
            rec.levelno == logging.WARNING
            and "call_id='missing'" in rec.message
            and "idx=0" in rec.message
            for rec in caplog.records
        )

    def test_empty_assistant_message_substitutes_placeholder(self):
        """Empty assistant message (no content, no tool_calls) is replaced with placeholder at the wire boundary.

        This is defense-in-depth: a corrupt Message in conv.messages (created before
        the write-side guard landed at agent/runtime.py:2222) should never produce
        an empty {"role":"assistant","content":""} entry, because strict providers
        (Cohere, strict OpenAI tool-loop, Anthropic strict mode) reject that with
        HTTP 400 "must have non-empty content or tool calls".
        """
        c = Conversation(agent_name="Coder")
        c.add_assistant_message("", [])
        msgs = c.to_api_messages()
        assert msgs == [
            {
                "role": "assistant",
                "content": "[assistant returned no content — placeholder]",
            }
        ]

    def test_empty_assistant_message_logs_warning(self, caplog):
        """Empty assistant message substitution emits a warning log with idx info."""
        import logging
        c = Conversation(agent_name="Coder")
        c.add_user_message("hello")
        c.add_assistant_message("", [])  # corrupt
        with caplog.at_level(logging.WARNING, logger="models.conversation"):
            c.to_api_messages()
        assert any(
            "empty assistant message" in rec.message
            and "idx=1" in rec.message
            and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), f"Expected WARNING with 'empty assistant message' and 'idx=1'. Got: {[r.message for r in caplog.records]}"

    def test_assistant_message_with_only_tool_calls_does_not_substitute(self):
        """Empty content + non-empty tool_calls is VALID; placeholder must NOT fire.

        Strict providers reject empty content with NO tool_calls, but they accept
        empty content WITH tool_calls (the content is optional when there are
        tool_calls). The Phase 1 guard must only fire when both are falsy.
        """
        c = Conversation(agent_name="Coder")
        tc = ToolCall(call_id="call_abc", tool_name="read_file", arguments={"path": "a.py"})
        c.add_assistant_message("", [tc])
        msgs = c.to_api_messages()
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"] == ""  # unchanged, not the placeholder
        assert msgs[0]["tool_calls"][0]["id"] == "call_abc"
        assert "[placeholder]" not in msgs[0]["content"]

    def test_legacy_calling_tools_phrase_is_not_sent_back(self):
        """A stored '[calling tools]' reply must not be taught to the model."""
        c = Conversation(agent_name="Coder")
        tc = ToolCall(call_id="call_abc", tool_name="read_file", arguments={"path": "a.py"})
        c.add_assistant_message("[calling tools]", [tc])
        msgs = c.to_api_messages()
        assert msgs[0]["content"] == ""
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "read_file"

    def test_assistant_message_with_only_content_does_not_substitute(self):
        """Non-empty content + no tool_calls is the normal text-only path; placeholder must NOT fire."""
        c = Conversation(agent_name="Coder")
        c.add_assistant_message("Here is the answer", [])
        msgs = c.to_api_messages()
        assert msgs == [{"role": "assistant", "content": "Here is the answer"}]

    def test_corrupt_message_mid_sequence_uses_correct_index_in_warning(self, caplog):
        """Corrupt message at non-zero position: warning reports output-list index, not input-list index.

        Sequence: [user('a'), user('b'), corrupt assistant, user('c')]
        Output: 0='a', 1='b', 2=corrupt, 3='c' → warning must say idx=2.
        """
        import logging
        c = Conversation(agent_name="Coder")
        c.add_user_message("a")
        c.add_user_message("b")
        c.add_assistant_message("", [])  # corrupt, will be at output index 2
        c.add_user_message("c")
        with caplog.at_level(logging.WARNING, logger="models.conversation"):
            c.to_api_messages()
        assert any(
            "empty assistant message" in rec.message
            and "idx=2" in rec.message
            for rec in caplog.records
        ), f"Expected WARNING with 'idx=2'. Got: {[r.message for r in caplog.records]}"


class TestConversationTokenEstimate:
    """Phase CB-4: tests use tolerance-based assertions because tiktoken counts differ from chars // 4.

    The conv.model defaults to "" (empty string), which is not a known model name.
    So _tiktoken_encoding_for("") falls back to the cl100k_base default encoding.
    """

    def test_empty_conversation_is_zero(self):
        c = Conversation(agent_name="Coder")
        # system_prompt is empty, no messages → 0 tokens
        assert c.get_token_estimate() == 0

    def test_system_prompt_counted(self):
        # Use realistic text (not "x" * 40, which tokenizes to 1 token with tiktoken
        # but 10 tokens with chars // 4). The system_prompt is the agent's
        # general instructions, which contains normal English words.
        prompt = "You are a helpful assistant that writes Python code."  # 11 words
        c = Conversation(agent_name="Coder", system_prompt=prompt, model="gpt-4o")
        # With tiktoken (cl100k_base for gpt-4o), this prompt is ~10-11 tokens.
        # With chars // 4 fallback, it would be ~14 tokens.
        # We assert a tolerance: actual is within ±30% of the tiktoken ground truth.
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        expected = len(enc.encode(prompt))
        actual = c.get_token_estimate()
        assert abs(actual - expected) <= 1, f"expected ~{expected} tokens, got {actual}"

    def test_messages_counted(self):
        c = Conversation(agent_name="Coder", model="gpt-4o")
        c.add_user_message("hello world")  # 11 chars
        # tiktoken says: "hello world" = 2 tokens (cl100k_base).
        # chars // 4 says: 11 // 4 = 2.
        # Both agree on this case.
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        expected_msg = len(enc.encode("hello world"))
        actual = c.get_token_estimate()
        assert actual == expected_msg, f"expected {expected_msg} tokens, got {actual}"

    def test_tool_call_args_and_result_counted(self):
        c = Conversation(agent_name="Coder", model="gpt-4o")
        tc = ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "a.py", "content": "xyzt"})
        c.add_tool_result("c1", "result here")
        # tokens counted from tool_calls arguments + result
        estimate = c.get_token_estimate()
        assert estimate > 0
        # Sanity: at least the "result here" + serialized arguments
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        min_expected = len(enc.encode("result here"))
        assert estimate >= min_expected, f"expected >= {min_expected} tokens, got {estimate}"


class TestTiktokenAccurate:
    """Phase CB-4 (BUG #5 fix): tiktoken-based accurate token estimation."""

    def test_known_openai_model_uses_tiktoken(self):
        """conv.model='gpt-4o' should use tiktoken.encoding_for_model('gpt-4o')."""
        c = Conversation(agent_name="Coder", system_prompt="hello", model="gpt-4o")
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        expected = len(enc.encode("hello"))
        assert c.get_token_estimate() == expected

    def test_provider_prefix_is_stripped(self):
        """conv.model='openai/gpt-4o' should strip 'openai/' prefix and use 'gpt-4o'."""
        c = Conversation(agent_name="Coder", system_prompt="hello", model="openai/gpt-4o")
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        expected = len(enc.encode("hello"))
        assert c.get_token_estimate() == expected

    def test_unknown_model_falls_back_to_default_encoding(self):
        """conv.model='unknown-xyz' should fall back to cl100k_base (not chars // 4)."""
        c = Conversation(agent_name="Coder", system_prompt="hello world", model="unknown-xyz")
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        expected = len(enc.encode("hello world"))
        actual = c.get_token_estimate()
        assert actual == expected, f"expected {expected} tokens, got {actual} (chars//4 would be {len('hello world') // 4})"

    def test_tiktoken_import_error_falls_back_to_chars(self, monkeypatch):
        """If tiktoken is not importable, fall back to chars // 4 (no crash)."""
        import sys
        monkeypatch.setitem(sys.modules, "tiktoken", None)
        from models import conversation as conv_module
        monkeypatch.setattr(conv_module, "_tiktoken_encoding_for", lambda m: None)
        c = Conversation(agent_name="Coder", system_prompt="x" * 40)
        assert c.get_token_estimate() == 10  # 40 // 4 = 10

    def test_breakdown_uses_tiktoken(self):
        """get_token_breakdown should return tiktoken-accurate counts for known models."""
        c = Conversation(agent_name="Coder", system_prompt="x" * 40, model="gpt-4o")
        c.add_user_message("hello")
        breakdown = c.get_token_breakdown(model_max_tokens=1000)
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        expected_system = len(enc.encode("x" * 40))
        expected_conv = len(enc.encode("hello"))
        assert breakdown["system_prompt_tokens"] == expected_system
        assert breakdown["conversation_tokens"] == expected_conv
        assert breakdown["total_used_tokens"] == expected_system + expected_conv


class TestConversationCostTracking:
    def test_record_usage_updates_totals(self):
        c = Conversation(agent_name="Coder")
        c.record_usage(tokens=1000, cost=0.02)
        assert c.total_tokens == 1000
        assert c.total_cost == 0.02

    def test_record_usage_is_cumulative(self):
        c = Conversation(agent_name="Coder")
        c.record_usage(tokens=1000, cost=0.02)
        c.record_usage(tokens=500, cost=0.01)
        assert c.total_tokens == 1500
        assert c.total_cost == 0.03

    def test_record_usage_sets_last_message_tokens(self):
        c = Conversation(agent_name="Coder")
        c.add_user_message("hi")
        c.add_assistant_message("hello", [])
        c.record_usage(tokens=200, cost=0.004)
        assert c.messages[-1].tokens_used == 200


# ═══════════════════════════════════════════════════════════════════════════════
#  HIGH-3: api_key not persisted in conversation files
# ═══════════════════════════════════════════════════════════════════════════════

import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from models.conversation import Conversation, Message, MessageRole


class TestSaveConversationDoesNotIncludeApiKey:
    """HIGH-3: _save_conversation_to_disk must NOT include api_key in saved JSON."""

    def test_save_does_not_include_api_key_field(self, tmp_path):
        """Direct test: the saved JSON data dict must not have an 'api_key' key."""
        from agent.persistence import save_conversation_to_disk

        # Patch _conversations_dir to use tmp_path
        with patch("agent.persistence.conversations_dir", return_value=str(tmp_path)):
            conv = Conversation(
                agent_name="test-agent",
                model="openai/gpt-4o",
                provider="openai",
                api_key="sk-SECRET-KEY-12345",
            )
            conv.add_user_message("hello")
            path = save_conversation_to_disk(conv, "test-high3-key")

        with open(path, "r") as f:
            data = json.load(f)

        assert "api_key" not in data, (
            "HIGH-3 VIOLATION: api_key was written to the conversation file. "
            f"Found keys: {list(data.keys())}"
        )
        assert data["model"] == "openai/gpt-4o"
        assert data["provider"] == "openai"

    def test_save_includes_provider_field(self, tmp_path):
        """HIGH-3: provider field must be saved for api_key re-resolution on load."""
        from agent.persistence import save_conversation_to_disk

        with patch("agent.persistence.conversations_dir", return_value=str(tmp_path)):
            conv = Conversation(
                agent_name="test-agent",
                model="openai/gpt-4o",
                provider="openai",
            )
            path = save_conversation_to_disk(conv, "test-provider-field")

        with open(path, "r") as f:
            data = json.load(f)

        assert "provider" in data, "provider field must be saved for re-resolution"
        assert data["provider"] == "openai"

    def test_save_file_mode_is_0600(self, tmp_path):
        """HIGH-3: conversation file must have mode 0o600 after write."""
        from agent.persistence import save_conversation_to_disk

        with patch("agent.persistence.conversations_dir", return_value=str(tmp_path)):
            conv = Conversation(agent_name="test-agent", model="test/model")
            path = save_conversation_to_disk(conv, "test-file-mode")

        # On POSIX, check mode; on non-POSIX this is skipped
        if hasattr(os, "stat"):
            st = os.stat(path)
            assert (st.st_mode & 0o777) == 0o600, (
                f"HIGH-3 VIOLATION: file mode is {oct(st.st_mode & 0o777)}, "
                "expected 0o600"
            )


class TestResolveApiKeyForConversation:
    """HIGH-3: _resolve_api_key_for_conversation must never read from saved file."""

    def test_resolve_from_explicit_provider(self):
        """When data has an explicit provider, look it up by name."""
        from agent.persistence import resolve_api_key_for_conversation

        mock_provider = MagicMock()
        mock_provider.name = "openai"
        mock_provider.api_key = "sk-PROVIDER-KEY"

        with patch("utils.providers_store.load_providers", return_value=[mock_provider]):
            result = resolve_api_key_for_conversation({
                "model": "openai/gpt-4o",
                "provider": "openai",
            })

        assert result == "sk-PROVIDER-KEY"

    def test_resolve_from_model_prefix_when_no_provider(self):
        """When data has no provider, extract it from the model prefix."""
        from agent.persistence import resolve_api_key_for_conversation

        mock_provider = MagicMock()
        mock_provider.name = "openai"
        mock_provider.api_key = "sk-FROM-MODEL-KEY"

        with patch("utils.providers_store.load_providers", return_value=[mock_provider]):
            result = resolve_api_key_for_conversation({
                "model": "openai/gpt-4o",
                # no "provider" key
            })

        assert result == "sk-FROM-MODEL-KEY"

    def test_resolve_returns_none_when_no_matching_provider(self):
        """When no provider matches, return None (not the saved api_key)."""
        from agent.persistence import resolve_api_key_for_conversation

        mock_provider = MagicMock()
        mock_provider.name = "openai"
        mock_provider.api_key = "sk-REAL-KEY"

        with patch("utils.providers_store.load_providers", return_value=[mock_provider]):
            result = resolve_api_key_for_conversation({
                "model": "unknown/model",
                "provider": "unknown-provider",
            })

        assert result is None, (
            "Should return None when no provider matches. "
            "Returning a key would mean reading from the saved file."
        )

    def test_resolve_returns_none_when_no_providers_configured(self):
        """When providers.yaml is empty/missing, return None."""
        from agent.persistence import resolve_api_key_for_conversation

        with patch("utils.providers_store.load_providers", return_value=[]):
            result = resolve_api_key_for_conversation({"model": "openai/gpt-4o"})

        assert result is None

