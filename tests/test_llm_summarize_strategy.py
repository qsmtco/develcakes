# tests/test_llm_summarize_strategy.py
# Tests for Phase C — LLM-Summarization Strategy.

import pytest
from unittest.mock import MagicMock, patch

from agent.config import AgentConfig


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


class TestLLMSummarizeStrategy:

    def test_no_provider_falls_back_to_textual(self):
        """When llm_provider is None, falls back to super()._summary()."""
        from agent.context_strategy import LLMSummarizeStrategy, DefaultContextStrategy
        strat = LLMSummarizeStrategy(llm_provider=None)
        conv = MagicMock()
        conv.messages = []
        conv.system_prompt = "test"
        # Should not crash — should return whatever super returns
        result = strat._summary(conv, token_budget=1000, keep_first=2)
        assert isinstance(result, str)

    def test_empty_llm_response_falls_back(self):
        """Empty LLM response falls back to textual."""
        from agent.context_strategy import LLMSummarizeStrategy
        from models.conversation import Message, MessageRole
        strat = LLMSummarizeStrategy(llm_provider=lambda sys_p, user_p: "")
        conv = MagicMock()
        conv.messages = [
            Message(role=MessageRole.USER, content="hello", tokens_used=10)
            for _ in range(10)
        ]
        conv.system_prompt = "test"
        conv.get_token_estimate.return_value = 5000
        result = strat._summary(conv, token_budget=1000, keep_first=2)
        assert isinstance(result, str)

    def test_llm_exception_falls_back(self):
        """LLM call raising exception falls back to textual."""
        from agent.context_strategy import LLMSummarizeStrategy
        from models.conversation import Message, MessageRole

        def raising_provider(sys_p, user_p):
            raise RuntimeError("network error")

        strat = LLMSummarizeStrategy(llm_provider=raising_provider)
        conv = MagicMock()
        conv.messages = [
            Message(role=MessageRole.USER, content="hello", tokens_used=10)
            for _ in range(10)
        ]
        conv.system_prompt = "test"
        conv.get_token_estimate.return_value = 5000
        result = strat._summary(conv, token_budget=1000, keep_first=2)
        assert isinstance(result, str)

    def test_successful_llm_summary_returned(self):
        """LLM returns a valid response — strategy returns it verbatim."""
        from agent.context_strategy import LLMSummarizeStrategy

        summary = "<task>Fix the bug</task><progress>Step 1 done</progress>"
        strat = LLMSummarizeStrategy(llm_provider=lambda sys_p, user_p: summary)
        conv = MagicMock()
        msg = MagicMock()
        msg.role = MagicMock(value="user")
        msg.content = "hello"
        conv.messages = [msg] * 10
        conv.system_prompt = "test"
        conv.get_token_estimate.return_value = 5000
        result = strat._summary(conv, token_budget=1000, keep_first=2)
        assert result == summary

    def test_too_few_messages_returns_empty(self):
        """When conversation has <= 4 messages, _summary returns empty string."""
        from agent.context_strategy import LLMSummarizeStrategy
        strat = LLMSummarizeStrategy(llm_provider=lambda sys_p, user_p: "should not be called")
        conv = MagicMock()
        conv.messages = [MagicMock()] * 3
        conv.system_prompt = "test"
        result = strat._summary(conv, token_budget=1000, keep_first=2)
        assert result == ""

    def test_no_double_truncation(self):
        """BUG #2: strategy should NOT truncate — parent handles fitting."""
        from agent.context_strategy import LLMSummarizeStrategy
        long_response = "<task>x</task>" + "a" * 5000
        strat = LLMSummarizeStrategy(llm_provider=lambda sys_p, user_p: long_response)
        conv = MagicMock()
        msg = MagicMock()
        msg.role = MagicMock(value="user")
        msg.content = "hello"
        conv.messages = [msg] * 10
        conv.system_prompt = "test"
        conv.get_token_estimate.return_value = 5000
        result = strat._summary(conv, token_budget=100, keep_first=2)
        # Should be the FULL response, not truncated by the strategy
        assert len(result) == len(long_response)
        assert "summary truncated" not in result


class TestForceLlmCompact:
    """Tests for AgentRuntime.force_llm_compact."""

    def test_shared_strategy_untouched_during_llm_compact(self):
        """SPEC-61 A4: force_llm_compact must not swap _context_strategy."""
        from agent.runtime import AgentRuntime
        from models.conversation import Message, MessageRole
        rt = AgentRuntime(_make_cfg())
        original = rt._context_strategy

        # A conversation over budget so compact() actually trims messages and
        # fires the LLM summary call (the provider is only invoked when
        # messages were removed).
        conv = MagicMock()
        conv.messages = [
            Message(role=MessageRole.USER, content="hello", tokens_used=10)
            for _ in range(10)
        ]
        conv.system_prompt = "test"
        conv.model = "openai/gpt-4o"
        conv.get_token_estimate.return_value = 10000

        recorded = []

        def fake_summary(*a, **kw):
            # Record whether the runtime-wide strategy is still the original
            # DURING the LLM summary call, then fail like a bad provider.
            recorded.append(rt._context_strategy is original)
            raise RuntimeError("no provider")

        with patch.object(rt, "_call_for_summary", side_effect=fake_summary):
            try:
                rt.force_llm_compact(conv, 5000, "")
            except Exception:
                pass  # the strategy falls back to a textual summary

        assert recorded, "expected the LLM summary call to run during compact"
        assert recorded[0] is True
        assert rt._context_strategy is original

    def test_system_prompt_restored(self):
        """force_llm_compact must restore conv.system_prompt after focus_text."""
        from agent.context_strategy import DefaultContextStrategy
        from agent.runtime import AgentRuntime
        rt = AgentRuntime.__new__(AgentRuntime)
        rt._context_strategy = DefaultContextStrategy()

        conv = MagicMock()
        conv.messages = []
        conv.system_prompt = "original prompt"
        conv.model = "openai/gpt-4o"
        conv.get_token_estimate.return_value = 100

        with patch.object(rt, "_call_for_summary", side_effect=RuntimeError("no provider")):
            try:
                rt.force_llm_compact(conv, 5000, "focus on auth")
            except Exception:
                pass

        assert conv.system_prompt == "original prompt"

    def test_dead_variables_removed(self):
        """BUG #6: messages_before and tokens_before should not exist in force_llm_compact."""
        import inspect
        from agent.runtime import AgentRuntime
        source = inspect.getsource(AgentRuntime.force_llm_compact)
        assert "messages_before" not in source
        assert "tokens_before" not in source


class TestCallForSummary:
    """Tests for AgentRuntime._call_for_summary."""

    def test_empty_model_raises(self):
        from agent.runtime import AgentRuntime
        rt = AgentRuntime.__new__(AgentRuntime)
        rt._config = MagicMock()
        rt._config.providers = {}
        rt._config.default_provider = None
        rt._config.default_model = None
        with pytest.raises(RuntimeError, match="no model_id"):
            rt._call_for_summary("sys", "user", model_id=None, conv=None)

    def test_no_slash_in_model_raises(self):
        from agent.runtime import AgentRuntime
        rt = AgentRuntime.__new__(AgentRuntime)
        with pytest.raises(RuntimeError, match="provider/model"):
            rt._call_for_summary("sys", "user", model_id="gpt-4o", conv=None)

    def test_unknown_provider_raises(self):
        from agent.runtime import AgentRuntime
        rt = AgentRuntime.__new__(AgentRuntime)
        rt._config = MagicMock()
        rt._config.providers = {}
        with pytest.raises(RuntimeError, match="not configured"):
            rt._call_for_summary("sys", "user", model_id="unknown/gpt-4o", conv=None)


class TestLlmNameResolution:
    """Tests for agent_def.llm_name precedence in force_llm_compact."""

    def test_llm_name_overrides_conv_model(self):
        """When agent_def has llm_name, it takes precedence over conv.model."""
        from agent.runtime import AgentRuntime
        from agent.context_strategy import DefaultContextStrategy
        from models.conversation import Message, MessageRole
        rt = AgentRuntime.__new__(AgentRuntime)
        rt._context_strategy = DefaultContextStrategy()
        rt._compaction_lock = __import__("threading").Lock()

        # Provide enough messages so the trim loop fires and _summary is called
        conv = MagicMock()
        conv.messages = [
            Message(role=MessageRole.USER, content=f"msg {i}", tokens_used=200)
            for i in range(10)
        ]
        conv.system_prompt = "test"
        conv.model = "openai/gpt-4o"
        conv.get_token_estimate.return_value = 20000

        agent_def = MagicMock()
        agent_def.llm_name = "anthropic"

        # Mock config with anthropic provider
        prov_cfg = MagicMock()
        prov_cfg.default_model = "claude-3-5-sonnet"
        rt._config = MagicMock()
        rt._config.providers = {"anthropic": prov_cfg}

        captured_model = {}

        def mock_call_summary(system_prompt, user_prompt, model_id=None, conv=None):
            captured_model["model_id"] = model_id
            raise RuntimeError("stop here")  # prevent actual compact

        with patch.object(rt, "_call_for_summary", side_effect=mock_call_summary):
            try:
                rt.force_llm_compact(conv, 5000, "", agent_def=agent_def)
            except RuntimeError:
                pass

        assert captured_model.get("model_id", "").startswith("anthropic/")

    def test_no_llm_name_falls_back_to_conv_model(self):
        """When agent_def has no llm_name, falls back to conv.model."""
        from agent.runtime import AgentRuntime
        from agent.context_strategy import DefaultContextStrategy
        from models.conversation import Message, MessageRole
        rt = AgentRuntime.__new__(AgentRuntime)
        rt._context_strategy = DefaultContextStrategy()
        rt._compaction_lock = __import__("threading").Lock()

        conv = MagicMock()
        conv.messages = [
            Message(role=MessageRole.USER, content=f"msg {i}", tokens_used=200)
            for i in range(10)
        ]
        conv.system_prompt = "test"
        conv.model = "openai/gpt-4o"
        conv.get_token_estimate.return_value = 20000

        agent_def = MagicMock()
        agent_def.llm_name = None

        rt._config = MagicMock()
        rt._config.providers = {}

        captured_model = {}

        def mock_call_summary(system_prompt, user_prompt, model_id=None, conv=None):
            captured_model["model_id"] = model_id
            raise RuntimeError("stop here")

        with patch.object(rt, "_call_for_summary", side_effect=mock_call_summary):
            try:
                rt.force_llm_compact(conv, 5000, "", agent_def=agent_def)
            except RuntimeError:
                pass

        assert captured_model.get("model_id") == "openai/gpt-4o"
