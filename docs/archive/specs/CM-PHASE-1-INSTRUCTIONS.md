# Phase 1: Create agent/context_strategy.py + Delegation Shims

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §0, §2.1.2, §2.1.6, §2.1.7
**Files to change:**
1. `agent/context_strategy.py` — NEW MODULE
2. `models/conversation.py` — modify `trim_to_token_limit()` and `_last_exchange_summary()` into delegation shims

**Goal:** Mechanically extract the bodies of `trim_to_token_limit()` and `_last_exchange_summary()` from `Conversation` into `DefaultContextStrategy`. No behavior changes. All existing tests must pass unchanged.

---

## Step 1: Create `agent/context_strategy.py`

Create a new file at `/path/to/projects/crabcakes/agent/context_strategy.py`.

The file must contain:

### 1a. Imports

```python
"""Pluggable context management strategy.

See docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md §0 and
docs/proposals/PROPOSAL-pluggable-context-strategy.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from models.conversation import (
    Conversation,
    Message,
    MessageRole,
    _tiktoken_encoding_for,
)
```

### 1b. CompactionEvent dataclass (per §2.8.1)

```python
@dataclass
class CompactionEvent:
    """One compaction cycle's outcome. Appended to per-session history.

    Fields:
        turn: Tool-loop iteration number (1-indexed) when the event fired.
        trigger: What caused compaction.
        layer: Compaction layer that fired (1=prune, 2=trim, 3=manual).
        messages_before: len(conv.messages) at start of compaction cycle.
        messages_after: len(conv.messages) at end of compaction cycle.
        messages_removed: messages_before - messages_after.
        tokens_before: get_token_estimate() before compaction.
        tokens_after: get_token_estimate() after compaction.
        tokens_freed: tokens_before - tokens_after.
        summary_tokens_injected: tokens used by injected summary (0 if none).
        soft_ceiling: The soft_ceiling used for this cycle (in tokens).
        hard_ceiling: The hard_ceiling used for this cycle (in tokens).
        provider: Provider name (e.g. "openai").
        model: Model id (e.g. "openai/gpt-4o").
    """
    turn: int
    trigger: str
    layer: int
    messages_before: int
    messages_after: int
    messages_removed: int
    tokens_before: int
    tokens_after: int
    tokens_freed: int
    summary_tokens_injected: int
    soft_ceiling: int
    hard_ceiling: int
    provider: str
    model: str
```

### 1c. ContextStrategy Protocol (per §0.2)

```python
class ContextStrategy(Protocol):
    """Pluggable compaction policy."""

    def compact(self, conv: Conversation, token_budget: int) -> None:
        """Reduce conv so its token estimate fits within token_budget."""
        ...

    @property
    def last_result(self) -> CompactionEvent | None:
        """Telemetry from the most recent compact() call."""
        ...
```

### 1d. DefaultContextStrategy class

This class holds the EXTRACTED bodies of `Conversation.trim_to_token_limit()` and `Conversation._last_exchange_summary()`. The extraction is mechanical: every `self.messages` becomes `conv.messages`, every `self.get_token_estimate()` becomes `conv.get_token_estimate()`, every `self._token_estimate_cache` becomes `conv._token_estimate_cache`, every `self.system_prompt` becomes `conv.system_prompt`.

**IMPORTANT:** For Phase 1, the `compact()` method body is the CURRENT trim_to_token_limit algorithm — no P2/P3/P6 enhancements yet. Those come in later phases. We are doing a mechanical extraction ONLY.

```python
class DefaultContextStrategy:
    """Default compaction strategy. See SPEC-CONTEXT-MANAGEMENT-ROADMAP.md §0.

    Phase 1: mechanical extraction from Conversation. No behavior changes.
    """

    def __init__(self) -> None:
        self._last_result: CompactionEvent | None = None

    @property
    def last_result(self) -> CompactionEvent | None:
        """Telemetry from the most recent compact() call."""
        return self._last_result

    def compact(
        self,
        conv: Conversation,
        token_budget: int,
        *,
        keep_first: int = 2,
        protect_is_summary: bool = True,
    ) -> None:
        """Compact the conversation to fit within token_budget.

        Phase 1: this is the mechanical extraction of trim_to_token_limit().
        The keep_first and protect_is_summary parameters are accepted but
        NOT YET USED (defaults preserve old behavior). P2/P3 enhancements
        come in Phase 4.
        """
        # Mechanical extraction from Conversation.trim_to_token_limit()
        # self -> conv throughout. No behavior changes.
        messages_count_before = len(conv.messages)
        tokens_before = conv.get_token_estimate()
        conv._token_estimate_cache = None
        summary_tokens_injected = 0

        while conv.get_token_estimate() > token_budget and len(conv.messages) > 4:
            removed = False
            for i in range(len(conv.messages) - 1, 0, -1):
                msg = conv.messages[i]
                if msg.role == MessageRole.TOOL_RESULT:
                    if i > 0 and conv.messages[i - 1].role == MessageRole.ASSISTANT and conv.messages[i - 1].tool_calls:
                        conv.messages.pop(i)
                        conv.messages.pop(i - 1)
                        removed = True
                        break
                elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                    if i + 1 < len(conv.messages) and conv.messages[i + 1].role == MessageRole.TOOL_RESULT:
                        conv.messages.pop(i + 1)
                        conv.messages.pop(i)
                        removed = True
                        break
            if not removed:
                tail_preserve = 4
                if len(conv.messages) > tail_preserve:
                    conv.messages.pop(0)
                else:
                    break

        # Summary injection (unchanged from current behavior)
        messages_removed = messages_count_before - len(conv.messages)
        if messages_removed > 0 and len(conv.messages) >= 4:
            summary = self._summary(conv)
            if summary:
                summary_tokens = len(summary) // 4
                summary_tokens_injected = summary_tokens
                current_tokens = conv.get_token_estimate()
                if current_tokens + summary_tokens > token_budget:
                    pass  # skip injection — would exceed budget
                else:
                    summary_msg = Message(role=MessageRole.ASSISTANT, content=summary, is_summary=True)
                    insert_at = max(1, len(conv.messages) - 4)
                    conv.messages.insert(insert_at, summary_msg)

        conv._token_estimate_cache = None
        tokens_after = conv.get_token_estimate()

        # Record telemetry (per §0.4)
        provider = ""
        model = conv.model or ""
        if "/" in model:
            provider, model = model.split("/", 1)

        self._last_result = CompactionEvent(
            turn=conv.step_count,
            trigger="trim",
            layer=2,
            messages_before=messages_count_before,
            messages_after=len(conv.messages),
            messages_removed=messages_count_before - len(conv.messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_freed=tokens_before - tokens_after,
            summary_tokens_injected=summary_tokens_injected,
            soft_ceiling=token_budget,
            hard_ceiling=0,  # not known at strategy level in Phase 1
            provider=provider,
            model=model,
        )

    def _summary(
        self,
        conv: Conversation,
        token_budget: int = 0,
        keep_first: int = 2,
    ) -> str:
        """Generate a summary of the oldest trimmed user messages.

        Phase 1: mechanical extraction of Conversation._last_exchange_summary().
        No behavior changes.
        """
        if not conv.messages:
            return ""

        tail_preserve = 4
        if len(conv.messages) <= tail_preserve:
            return ""

        user_contents: list[str] = []
        for msg in conv.messages[:-tail_preserve]:
            if msg.role == MessageRole.USER:
                user_contents.append(msg.content.strip())

        if not user_contents:
            return ""

        lines = [f"Conversation so far ({len(user_contents)} prior turns):"]
        for i, content in enumerate(user_contents[:5], 1):
            preview = content[:100] + ("…" if len(content) > 100 else "")
            lines.append(f"  {i}. {preview}")
        if len(user_contents) > 5:
            lines.append(f"  … and {len(user_contents) - 5} more turns")

        return "\n".join(lines)
```

**Key notes:**
- `conv.step_count` is used for the `turn` field. Check that `Conversation` has `step_count` — if not, use `0` as a placeholder.
- The `_summary` method signature includes `token_budget` and `keep_first` for forward-compat, but Phase 1 ignores them (same as current behavior).
- `hard_ceiling=0` in Phase 1 because the strategy doesn't know the hard ceiling yet. This gets fixed when the runtime passes it in a later phase.

---

## Step 2: Modify `models/conversation.py` — Add Delegation Shims

Replace the BODY of `trim_to_token_limit()` (lines 365-456) with a thin delegation shim:

```python
def trim_to_token_limit(
    self,
    max_tokens: int,
    *,
    keep_first: int = 2,
    protect_is_summary: bool = True,
) -> None:
    """Trim oldest messages to stay under token limit.

    .. deprecated:: 2026-06-26
        Use ``DefaultContextStrategy.compact()`` instead. This shim delegates
        to the strategy for backward compatibility with existing tests.
    """
    from agent.context_strategy import DefaultContextStrategy
    strategy = DefaultContextStrategy()
    strategy.compact(self, max_tokens, keep_first=keep_first, protect_is_summary=protect_is_summary)
```

Replace the BODY of `_last_exchange_summary()` (lines 458-498) with a thin delegation shim:

```python
def _last_exchange_summary(self, *, max_tokens: int = 0, keep_first: int = 2) -> str:
    """Generate a summary of the oldest trimmed user messages.

    .. deprecated:: 2026-06-26
        Use ``DefaultContextStrategy._summary()`` instead. This shim delegates
        to the strategy for backward compatibility.
    """
    from agent.context_strategy import DefaultContextStrategy
    strategy = DefaultContextStrategy()
    return strategy._summary(self, max_tokens, keep_first)
```

**CRITICAL RULES:**
- Do NOT change any other method in `conversation.py`.
- Do NOT remove any imports from `conversation.py`.
- Do NOT add any new imports at the module level (the shim uses a deferred import inside the method body to avoid circular import between `models/` and `agent/`).
- Keep the docstrings but mark them as deprecated.
- The existing 14 tests in `TestConversationTrim`, `TestTrimFallbackIncludesOldest`, and `TestTrimSummaryInjection` MUST pass unchanged.

---

## Verification

After making changes, run:

```bash
cd /path/to/projects/crabcakes

# 1. Module imports cleanly
python3 -c "from agent.context_strategy import DefaultContextStrategy, ContextStrategy, CompactionEvent; print('imports OK')"

# 2. Conversation shim works
python3 -c "
from models.conversation import Conversation
c = Conversation(agent_name='test')
c.add_user_message('hello')
c.add_assistant_message('hi', [])
c.trim_to_token_limit(max_tokens=100000)
print('shim OK, messages:', len(c.messages))
"

# 3. ALL existing tests pass unchanged
python3 -m pytest tests/test_conversation.py tests/test_phase4.py -v --tb=short 2>&1 | tail -40

# 4. No regressions in full suite
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

After implementation, report:

```
COMPLETENESS:
- [x/not done] Created agent/context_strategy.py with ContextStrategy protocol — evidence (line N)
- [x/not done] Created CompactionEvent dataclass in agent/context_strategy.py — evidence (line N)
- [x/not done] Created DefaultContextStrategy.compact() — mechanical extraction from trim_to_token_limit — evidence (line N)
- [x/not done] Created DefaultContextStrategy._summary() — mechanical extraction from _last_exchange_summary — evidence (line N)
- [x/not done] Created DefaultContextStrategy.last_result property — evidence (line N)
- [x/not done] Modified Conversation.trim_to_token_limit() to delegation shim — evidence (line N)
- [x/not done] Modified Conversation._last_exchange_summary() to delegation shim — evidence (line N)
- [x/not done] All 14 existing trim/summary tests pass — evidence (pytest output)
- [x/not done] Full test suite has no regressions — evidence (pytest output)
- [x/not done] No new imports at module level in conversation.py — evidence (grep output)
```

## Related issues to flag (do NOT fix in this phase):
- P2 keep_first enforcement (Phase 4)
- P3 protect_is_summary enforcement (Phase 4)
- P4 prune_tool_outputs (Phase 5)
- P5 _find_split_index (Phase 6)
- P6 _fit_summary (Phase 6)
- P7 dynamic prompt budget (Phase 7)
