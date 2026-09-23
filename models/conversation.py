import json
import logging
# models/conversation.py
# Conversation and Message data models for the agent runtime.
#
# Manifest: reads nothing, writes nothing, no network.
# Architecture: pure data — no GTK, no network, no LLM calls.
# All imports are stdlib only (dataclasses, datetime, enum, typing).
#
# Files that import these:
#   - agent/runtime.py (owns Conversation instances)
#   - agent/context.py (reads Conversation for context building)
#   - tests/test_conversation.py (unit tests)

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable


# Phase CB-4: tiktoken-based accurate token estimation (BUG #5 fix).
# See SPEC-CONTEXT-BLOAT-PHASE-4.md §2.2.

_DEFAULT_ENCODING_NAME = "cl100k_base"  # GPT-4 / GPT-3.5-turbo encoding; reasonable proxy for non-OpenAI models

_logger = logging.getLogger(__name__)


def _tiktoken_encoding_for(model) -> object | None:
    """
    Return the tiktoken encoding for the given model name, or None on failure.

    Resolution order:
      1. tiktoken.encoding_for_model(model_name)  (OpenAI models)
      2. tiktoken.get_encoding("cl100k_base")    (default for non-OpenAI models)

    Returns None if tiktoken is not installed, the model is not a string,
    or any other exception occurs.
    The caller must fall back to the chars // 4 heuristic when None is returned.

    Strips provider prefix from model names like "openai/gpt-4o" → "gpt-4o".
    """
    if not isinstance(model, str) or not model:
        # Non-string or empty — skip encoding_for_model and fall through to cl100k_base default.
        bare_name = ""
    else:
        bare_name = model.split("/", 1)[-1] if "/" in model else model

    try:
        import tiktoken
    except ImportError:
        return None

    try:
        if bare_name:
            return tiktoken.encoding_for_model(bare_name)
        # Empty/None model — skip encoding_for_model (it requires a non-empty
        # string) and fall through to the cl100k_base default.
        raise KeyError("empty model name")

    except KeyError:
        try:
            return tiktoken.get_encoding(_DEFAULT_ENCODING_NAME)
        except Exception:
            return None
    except Exception:
        return None


# ── Enums ─────────────────────────────────────────────────────────────────────


class MessageRole(str, Enum):
    """Role of a message in a conversation."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool"


class ToolCallStatus(str, Enum):
    """Lifecycle stage of a tool call."""
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Data models ────────────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    """A single tool invocation from an assistant message."""
    call_id: str                          # unique identifier (e.g. "call_abc123")
    tool_name: str                        # e.g. "read_file", "exec_command"
    arguments: dict                       # parsed JSON arguments as dict
    result: str | None = None            # tool execution result text
    status: ToolCallStatus = ToolCallStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def mark_executing(self) -> None:
        self.status = ToolCallStatus.EXECUTING
        self.started_at = datetime.now()

    def mark_completed(self, result: str) -> None:
        self.status = ToolCallStatus.COMPLETED
        self.result = result
        self.completed_at = datetime.now()

    def mark_failed(self, error: str) -> None:
        self.status = ToolCallStatus.FAILED
        self.result = error
        self.completed_at = datetime.now()


@dataclass
class Message:
    """A single turn in a conversation."""
    role: MessageRole
    content: str                          # empty string for tool-call messages
    tool_calls: list[ToolCall] = field(default_factory=list)   # non-empty only for assistant messages
    tool_call_id: str | None = None      # non-None only for tool-result messages
    timestamp: datetime = field(default_factory=datetime.now)
    tokens_used: int = 0                 # tokens consumed by this message
    is_summary: bool = False             # True for injected summary-on-trim messages

    @property
    def is_tool_call(self) -> bool:
        """True if this is an assistant message that triggered tool calls."""
        return self.role == MessageRole.ASSISTANT and bool(self.tool_calls)

    @property
    def is_tool_result(self) -> bool:
        """True if this is a tool-result message."""
        return self.role == MessageRole.TOOL_RESULT


@dataclass
class Conversation:
    """Full conversation state for a single agent tab.

    The session_key (not stored here) is the lookup key used by AgentRuntime.
    This dataclass holds everything that persists across turns.
    """
    agent_name: str
    agent_role: str = ""          # role from agent def (e.g. "coder", "")
    # project_path: The directory the agent is "working in". Written to disk
    # by _save_conversation_to_disk for audit, but NOT authoritative at load
    # time — see _load_conversation_from_disk (which always sets it to None)
    # and AgentRuntime._rebuild_conversation_context (which re-applies the
    # currently-active project). The on-disk value may be stale if the
    # user switched projects between sessions. Touching this field directly
    # will not re-apply the change to tool sandboxing or the system prompt;
    # always go through _rebuild_conversation_context for those.
    project_path: str | None = None
    allowed_tools: list[str] | None = None  # filtered tool set — None means all tools
    mcp_servers: list[str] = field(default_factory=list)  # MCP servers for this conversation
    # system_prompt: The fully-rendered prompt sent as the first message
    # in every LLM call. Written to disk for audit, but NOT authoritative
    # at load time — _load_conversation_from_disk always sets this to ""
    # and AgentRuntime._rebuild_conversation_context rebuilds it against
    # the currently-active project. Same persistence-is-not-authoritative
    # invariant as `project_path` above (audit-only on disk; runtime rebuilds
    # via `_rebuild_conversation_context`).
    system_prompt: str = ""
    messages: list[Message] = field(default_factory=list)
    model: str = ""                      # e.g. "openai/gpt-4o"
    provider: str | None = None           # provider name for api_key re-resolution (HIGH-3)
    api_key: str | None = None           # per-agent API key override (from agent def)
    si_enforcement: bool | None = None     # per-agent enforcement override (None → use global)
    app_title: str = ""                   # OpenRouter X-Title header value (e.g. "Coder:Crabcakes")
    fallback_provider: str | None = None   # plain provider fallback (user-set)
    fallback_model: str | None = None      # plain provider fallback model
    created_at: datetime = field(default_factory=datetime.now)
    total_tokens: int = 0
    total_cost: float = 0.0              # cumulative USD cost
    step_count: int = 0                  # number of complete agent turns

    # Phase CB-5: token-estimate cache (BUG #1 fix from end-to-end audit).
    # Invalidated by any message add/remove/trim operation. Keyed on
    # (len(messages), hash(system_prompt)). See get_token_estimate().
    _token_estimate_cache: tuple | None = field(default=None, repr=False, compare=False)

    # ── Message helpers ───────────────────────────────────────────────────────

    def add_user_message(self, content: str) -> Message:
        """Add a user (PM) message and return it."""
        msg = Message(role=MessageRole.USER, content=content)
        self.messages.append(msg)
        # Phase CB-5: invalidate token cache on any message addition
        self._token_estimate_cache = None
        return msg

    def add_assistant_message(
        self,
        content: str,
        tool_calls: list[ToolCall] | None = None,
    ) -> Message:
        """Add an assistant message and return it."""
        msg = Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls or [],
        )
        self.messages.append(msg)
        self.step_count += 1
        self._token_estimate_cache = None
        return msg

    def add_tool_result(self, call_id: str, result: str) -> Message:
        """Add a tool-result message and return it."""
        msg = Message(
            role=MessageRole.TOOL_RESULT,
            content=result,
            tool_call_id=call_id,
        )
        self.messages.append(msg)
        self._token_estimate_cache = None
        return msg

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_api_messages(self) -> list[dict]:
        """
        Serialize conversation to LLM API format.

        Returns:
            [{"role": "system", "content": "..."},
             {"role": "user", "content": "..."},
             {"role": "assistant", "content": "text", "tool_calls": [...]},
             {"role": "tool", "tool_call_id": "...", "content": "..."},
             ...]

        Rules:
        - System prompt becomes the first system message
        - Tool calls from assistant messages are serialized as OpenAI-style dicts
        - Tool results use tool_call_id to link back
        """
        result: list[dict] = []

        if self.system_prompt:
            result.append({"role": "system", "content": self.system_prompt})

        for msg in self.messages:
            if msg.role == MessageRole.USER:
                result.append({"role": "user", "content": msg.content})

            elif msg.role == MessageRole.ASSISTANT:
                # Defense in depth: strict providers (Cohere, OpenAI tool-loop,
                # Anthropic strict mode) reject {"role":"assistant","content":""}
                # with HTTP 400 "must have non-empty content or tool calls".
                # A corrupt message can exist in conv.messages if it was created
                # before the write-side guard landed, or via some other path we
                # haven't audited. Substitute a descriptive placeholder at the
                # wire boundary so the call succeeds. The original Message stays
                # in conv.messages (audit trail preserved); only the serialized
                # form changes.
                if not msg.content and not msg.tool_calls:
                    _logger.warning(
                        "to_api_messages: empty assistant message at idx=%d "
                        "(role=ASSISTANT, content='', tool_calls=[]) — "
                        "substituting placeholder to satisfy strict providers",
                        len(result),
                    )
                    entry = {
                        "role": "assistant",
                        "content": "[assistant returned no content — placeholder]",
                    }
                else:
                    entry = {"role": "assistant", "content": msg.content}
                    if msg.tool_calls:
                        entry["tool_calls"] = [
                            {
                                "id": tc.call_id,
                                "type": "function",
                                "function": {
                                    "name": tc.tool_name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in msg.tool_calls
                        ]
                result.append(entry)

            elif msg.role == MessageRole.TOOL_RESULT:
                result.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content,
                })

        return result

    # ── Token management ───────────────────────────────────────────────────────

    def _count_char_tokens(self) -> tuple[int, int]:
        """Return (system_prompt_chars, conversation_chars) for token estimation."""
        system_chars = len(self.system_prompt)
        conv_chars = 0
        for msg in self.messages:
            conv_chars += len(msg.content)
            for tc in msg.tool_calls:
                conv_chars += len(str(tc.arguments))
                if tc.result:
                    conv_chars += len(tc.result)
        return system_chars, conv_chars

    def get_token_estimate(self) -> int:
        """
        Token count estimate for the conversation.

        Phase CB-4: when tiktoken is installed and the model has a known encoding,
        uses tiktoken.encoding_for_model() for accurate counts. Falls back to the
        chars // 4 heuristic for unknown models or when tiktoken is unavailable.

        Phase CB-5: caches the tiktoken result to avoid re-encoding on every call
        (the trim loop calls this once per iteration; without caching, a 100K-char
        system prompt makes each call take ~6s).

        Used for context-window management (see
        DefaultContextStrategy.compact in agent/context_strategy.py).
        """
        encoding = _tiktoken_encoding_for(self.model)
        if encoding is None:
            # Fallback path — fast (string length), no caching needed.
            system_chars, conv_chars = self._count_char_tokens()
            return (system_chars + conv_chars) // 4

        # Tiktoken path — check cache.
        cache_key = (len(self.messages), hash(self.system_prompt))
        if self._token_estimate_cache is not None:
            cached_key, cached_value = self._token_estimate_cache
            if cached_key == cache_key:
                return cached_value

        result = self._count_tokens_accurate(encoding)
        self._token_estimate_cache = (cache_key, result)
        return result

    def _count_tokens_accurate(self, encoding) -> int:
        """
        Count tokens accurately using the provided tiktoken encoding.

        Counts tokens in:
        - system_prompt
        - each message's content
        - each tool_call's arguments (serialized) and result

        Does NOT count tool_call.name (it doesn't appear in the API request body
        sent to the LLM, only the id and arguments do).
        """
        total = len(encoding.encode(self.system_prompt))
        for msg in self.messages:
            total += len(encoding.encode(msg.content or ""))
            for tc in msg.tool_calls:
                total += len(encoding.encode(str(tc.arguments)))
                if tc.result:
                    total += len(encoding.encode(tc.result))
        return total

    def get_token_breakdown(self, model_max_tokens: int) -> dict:
        """
        §4.15 — Per-turn token budget breakdown.

        Returns a dict with token allocation info for observability:
        - system_prompt_tokens: accurate count when tiktoken is available, else chars // 4
        - conversation_tokens: accurate count when tiktoken is available, else chars // 4
        - total_used_tokens: system + conversation
        - model_max_tokens: total available context window
        - remaining_tokens: model_max - total_used
        - usage_percent: (total_used / model_max_tokens) * 100

        Phase CB-4: uses tiktoken when available, falls back to chars // 4 otherwise.
        Same fallback semantics as get_token_estimate.

        This helps identify context bloat before hitting the limit.
        """
        encoding = _tiktoken_encoding_for(self.model)
        if encoding is not None:
            system_tokens = len(encoding.encode(self.system_prompt))
            conversation_tokens = 0
            for msg in self.messages:
                conversation_tokens += len(encoding.encode(msg.content or ""))
                for tc in msg.tool_calls:
                    conversation_tokens += len(encoding.encode(str(tc.arguments)))
                    if tc.result:
                        conversation_tokens += len(encoding.encode(tc.result))
        else:
            # Fallback: chars // 4 heuristic
            system_chars, conv_chars = self._count_char_tokens()
            system_tokens = system_chars // 4
            conversation_tokens = conv_chars // 4
        total_used = system_tokens + conversation_tokens
        return {
            "system_prompt_tokens": system_tokens,
            "conversation_tokens": conversation_tokens,
            "total_used_tokens": total_used,
            "model_max_tokens": model_max_tokens,
            "remaining_tokens": max(0, model_max_tokens - total_used),
            "usage_percent": round(total_used / model_max_tokens * 100, 1) if model_max_tokens > 0 else 0,
        }

    # ── Cost tracking ─────────────────────────────────────────────────────────

    def record_usage(self, tokens: int, cost: float) -> None:
        """Record tokens used and cost for a single LLM call."""
        self.total_tokens += tokens
        self.total_cost += cost
        if self.messages:
            self.messages[-1].tokens_used = tokens
