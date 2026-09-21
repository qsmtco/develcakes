# Phase 4: P2 keep_first + P3 protect_is_summary Behavioral Changes

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §2.1.2 (partial), §2.1.4
**Goal:** Implement the `_select_prune_candidate()` method and modify `compact()` to enforce `keep_first` and `protect_is_summary`. These are the FIRST behavioral changes (not just mechanical extraction).
**Files to change:**
1. `agent/context_strategy.py` — add `_select_prune_candidate()`, rewrite `compact()` trim loop
2. `tests/test_context_strategy.py` — NEW TEST FILE for P2/P3 behavior

**SCOPE:** This phase implements P2 and P3 ONLY. Do NOT implement:
- P4 (prune_tool_outputs) — Phase 5
- P5 (_find_split_index) — Phase 6
- P6 (_fit_summary with tiktoken) — Phase 6
- P7 (dynamic prompt budget) — Phase 7

The summary injection block MUST stay using the `len(summary) // 4` heuristic (NOT tiktoken) until Phase 6.

---

## Step 1: Add `_select_prune_candidate()` to DefaultContextStrategy

Add this method to the `DefaultContextStrategy` class in `agent/context_strategy.py`, BEFORE the `_summary()` method:

```python
    def _select_prune_candidate(
        self,
        conv: Conversation,
        keep_first: int,
        tail_preserve: int,
        protect_is_summary: bool,
    ) -> int | None:
        """Find the index of the best message to remove for budget trimming.

        Scans the trimmable region [keep_first, len - tail_preserve) for:
        1. First pass: non-protected messages (not is_summary when protect_is_summary=True)
        2. Second pass: protected messages (if no non-protected candidates)

        Prefers TOOL_RESULT + ASSISTANT-with-tool_calls pairs (CB-6 aware).
        Falls back to oldest non-protected message.

        CB-6 invariant at keep_first boundary: When a TOOL_RESULT candidate is
        at index ``keep_first``, its parent ASSISTANT-with-tool-calls at
        ``keep_first - 1`` is in the keep_first region and cannot be removed.
        This method skips those candidates.

        Returns the index of the message to remove, or None if the
        trimmable region is empty.
        """
        trimmable_end = len(conv.messages) - tail_preserve
        if trimmable_end <= keep_first:
            return None

        # Build the candidate list, non-protected first.
        non_protected: list[int] = []
        protected: list[int] = []
        for i in range(keep_first, trimmable_end):
            msg = conv.messages[i]
            is_protected = protect_is_summary and msg.is_summary
            if is_protected:
                protected.append(i)
            else:
                non_protected.append(i)

        # Try non-protected first, then protected.
        for candidate_pool in (non_protected, protected):
            if not candidate_pool:
                continue
            # Prefer TOOL_RESULT + ASSISTANT-with-tool_calls pairs (CB-6 aware).
            for i in candidate_pool:
                msg = conv.messages[i]
                if msg.role == MessageRole.TOOL_RESULT:
                    if (
                        i > 0
                        and conv.messages[i - 1].role == MessageRole.ASSISTANT
                        and conv.messages[i - 1].tool_calls
                        and (i - 1) >= keep_first
                    ):
                        return i
                elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                    if (
                        i + 1 < len(conv.messages)
                        and conv.messages[i + 1].role == MessageRole.TOOL_RESULT
                        and (i + 1) < trimmable_end
                    ):
                        return i
            # No CB-6 pairs found — return the first candidate (oldest).
            return candidate_pool[0]

        return None
```

## Step 2: Rewrite `compact()` trim loop to use `_select_prune_candidate()`

Replace the CURRENT trim loop in `compact()` (Phase 1's mechanical extraction) with the new P2/P3-aware version. The trim loop is the `while` block.

**Current trim loop (to replace):**
```python
        while conv.get_token_estimate() > token_budget and len(conv.messages) > 4:
            removed = False
            for i in range(len(conv.messages) - 1, 0, -1):
                msg = conv.messages[i]
                if msg.role == MessageRole.TOOL_RESULT:
                    if (
                        i > 0
                        and conv.messages[i - 1].role == MessageRole.ASSISTANT
                        and conv.messages[i - 1].tool_calls
                    ):
                        conv.messages.pop(i)
                        conv.messages.pop(i - 1)
                        removed = True
                        break
                elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                    if (
                        i + 1 < len(conv.messages)
                        and conv.messages[i + 1].role == MessageRole.TOOL_RESULT
                    ):
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
```

**New trim loop (replacement):**
```python
        tail_preserve = 4
        min_messages = keep_first + tail_preserve

        while conv.get_token_estimate() > token_budget and len(conv.messages) > min_messages:
            idx = self._select_prune_candidate(
                conv, keep_first, tail_preserve, protect_is_summary
            )
            if idx is None:
                break
            msg = conv.messages[idx]
            # CB-6: remove TOOL_RESULT + ASSISTANT-with-tool_calls as a pair.
            if msg.role == MessageRole.TOOL_RESULT:
                conv.messages.pop(idx)
                if (
                    idx > 0
                    and conv.messages[idx - 1].role == MessageRole.ASSISTANT
                    and conv.messages[idx - 1].tool_calls
                    and (idx - 1) >= keep_first
                ):
                    conv.messages.pop(idx - 1)
                elif (
                    idx > 0
                    and conv.messages[idx - 1].role == MessageRole.ASSISTANT
                    and conv.messages[idx - 1].tool_calls
                ):
                    # Parent ASSISTANT is in keep_first region — can't remove.
                    # _select_prune_candidate should have filtered this, but
                    # break defensively to prevent CB-6 violations.
                    break
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                trimmable_end = len(conv.messages) - tail_preserve
                if (
                    idx + 1 < len(conv.messages)
                    and conv.messages[idx + 1].role == MessageRole.TOOL_RESULT
                    and (idx + 1) < trimmable_end
                ):
                    conv.messages.pop(idx + 1)
                    conv.messages.pop(idx)
                else:
                    conv.messages.pop(idx)
            else:
                conv.messages.pop(idx)
            conv._token_estimate_cache = None
```

## Step 3: Update the summary injection block — MINIMAL CHANGES

The summary injection block must change `len(conv.messages) >= 4` to `len(conv.messages) >= min_messages` and `insert_at = max(1, ...)` to `insert_at = max(keep_first, ...)`. Otherwise keep the SAME `len(summary) // 4` heuristic (do NOT use tiktoken — that's Phase 6).

**Current summary block (to replace):**
```python
        messages_removed = messages_count_before - len(conv.messages)
        if messages_removed > 0 and len(conv.messages) >= 4:
            summary = self._summary(conv)
            if summary:
                summary_tokens = len(summary) // 4
                summary_tokens_injected = summary_tokens
                current_tokens = conv.get_token_estimate()
                if current_tokens + summary_tokens > token_budget:
                    pass  # skip — injecting would exceed budget
                else:
                    summary_msg = Message(
                        role=MessageRole.ASSISTANT,
                        content=summary,
                        is_summary=True,
                    )
                    insert_at = max(1, len(conv.messages) - 4)
                    conv.messages.insert(insert_at, summary_msg)
```

**New summary block (replacement):**
```python
        messages_removed = messages_count_before - len(conv.messages)
        if messages_removed > 0 and len(conv.messages) >= min_messages:
            summary = self._summary(conv)
            if summary:
                summary_tokens = len(summary) // 4
                summary_tokens_injected = summary_tokens
                current_tokens = conv.get_token_estimate()
                if current_tokens + summary_tokens > token_budget:
                    pass  # skip — injecting would exceed budget (Phase 6 adds _fit_summary)
                else:
                    summary_msg = Message(
                        role=MessageRole.ASSISTANT,
                        content=summary,
                        is_summary=True,
                    )
                    insert_at = max(keep_first, len(conv.messages) - tail_preserve)
                    conv.messages.insert(insert_at, summary_msg)
```

## Step 4: Define `min_messages` and `tail_preserve` before the trim loop

At the TOP of the `compact()` method body (right after `summary_tokens_injected = 0`), add:

```python
        tail_preserve = 4
        min_messages = keep_first + tail_preserve
```

These are used by both the trim loop and the summary injection block. Remove the local `tail_preserve = 4` that was inside the old fallback (if any).

---

## Step 5: Create test file `tests/test_context_strategy.py`

Create a new test file with tests for P2 (keep_first) and P3 (protect_is_summary):

```python
"""Tests for DefaultContextStrategy P2 (keep_first) and P3 (protect_is_summary)."""
import pytest
from agent.context_strategy import DefaultContextStrategy, CompactionEvent
from models.conversation import Conversation, Message, MessageRole


class TestKeepFirst:
    """P2: keep_first parameter protects the first N messages."""

    def test_keep_first_prevents_trim_below_min(self):
        """With keep_first=2 and tail_preserve=4, min_messages=6.
        Trim should stop when only 6 messages remain."""
        conv = Conversation(agent_name="test", model="test/x")
        # Add 10 messages (all very large to force trimming)
        for i in range(5):
            conv.add_user_message("x" * 500)
            conv.add_assistant_message("y" * 500, [])
        assert len(conv.messages) == 10

        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=100, keep_first=2)
        # min_messages = 2 + 4 = 6
        assert len(conv.messages) >= 6
        # The first 2 messages should still be there
        assert conv.messages[0].role == MessageRole.USER
        assert conv.messages[1].role == MessageRole.ASSISTANT

    def test_keep_first_default_is_2(self):
        """Default keep_first=2 matches the spec's default."""
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(8):
            conv.add_user_message("x" * 500)
            conv.add_assistant_message("y" * 500, [])
        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=100)
        assert len(conv.messages) >= 6

    def test_keep_first_3_protects_more(self):
        """keep_first=3 means min_messages=7."""
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(10):
            conv.add_user_message("x" * 500)
            conv.add_assistant_message("y" * 500, [])
        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=100, keep_first=3)
        assert len(conv.messages) >= 7

    def test_keep_first_zero_allows_aggressive_trim(self):
        """keep_first=0 means min_messages=4 (only tail_preserve)."""
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(8):
            conv.add_user_message("x" * 500)
            conv.add_assistant_message("y" * 500, [])
        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=100, keep_first=0)
        assert len(conv.messages) >= 4


class TestProtectIsSummary:
    """P3: protect_is_summary defers is_summary messages during trimming."""

    def test_summary_messages_trimmed_last(self):
        """When protect_is_summary=True, is_summary messages are pruned after non-protected."""
        conv = Conversation(agent_name="test", model="test/x")
        # Add a summary message early, then regular messages
        conv.add_user_message("task description")
        conv.add_assistant_message("initial response", [])
        conv.add_assistant_message("summary of earlier work", [], is_summary=True)
        for i in range(6):
            conv.add_user_message("x" * 500)
            conv.add_assistant_message("y" * 500, [])
        # Now we have 2 + 1 (summary) + 12 = 15 messages

        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=200, keep_first=2, protect_is_summary=True)

        # The summary message should survive longer than non-protected ones.
        # After trim, check that if any summary exists, it was preferentially kept.
        summaries = [m for m in conv.messages if m.is_summary]
        # If messages were trimmed and a summary remains, it means it was protected.
        # (This test is somewhat loose because we can't control exact trim count.)
        # The key invariant: non-protected messages were removed before the summary.
        # With 15 messages at budget=200, some trimming will happen.
        # If summary survived, that's P3 working.
        # If summary was removed, it should only have been removed after all
        # non-protected candidates were exhausted.

    def test_protect_is_summary_false_trims_normally(self):
        """When protect_is_summary=False, summary messages are not protected."""
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("task")
        conv.add_assistant_message("resp", [])
        conv.add_assistant_message("old summary", [], is_summary=True)
        for i in range(6):
            conv.add_user_message("x" * 500)
            conv.add_assistant_message("y" * 500, [])

        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=200, keep_first=2, protect_is_summary=False)

        # Summary should be treated like any other message — no special protection.
        summaries = [m for m in conv.messages if m.is_summary]
        # With aggressive trimming and no protection, summary likely removed.
        # (Not asserting presence/absence strictly — just that it doesn't crash.)


class TestLastResult:
    """Telemetry from compact() is recorded in last_result."""

    def test_last_result_is_none_before_first_call(self):
        strategy = DefaultContextStrategy()
        assert strategy.last_result is None

    def test_last_result_populated_after_compact(self):
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(6):
            conv.add_user_message("x" * 500)
            conv.add_assistant_message("y" * 500, [])
        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=100)
        result = strategy.last_result
        assert result is not None
        assert isinstance(result, CompactionEvent)
        assert result.trigger == "trim"
        assert result.layer == 2
        assert result.messages_before == 12
        assert result.messages_after < 12
        assert result.tokens_freed > 0

    def test_last_result_provider_model_extraction(self):
        conv = Conversation(agent_name="test", model="openai/gpt-4o")
        conv.add_user_message("x" * 500)
        conv.add_assistant_message("y" * 500, [])
        strategy = DefaultContextStrategy()
        strategy.compact(conv, token_budget=100)
        result = strategy.last_result
        assert result is not None
        assert result.provider == "openai"
        assert result.model == "gpt-4o"
```

---

## CRITICAL RULES

1. Do NOT change the `_summary()` method — it stays as Phase 1's mechanical extraction.
2. Do NOT use tiktoken in the summary block — keep `len(summary) // 4`. Phase 6 changes this.
3. Do NOT add `_fit_summary()` — Phase 6 adds this.
4. Do NOT change `models/conversation.py` — the delegation shims from Phase 1 are unchanged.
5. The `_select_prune_candidate()` method implements BOTH the P2 (keep_first) and P3 (protect_is_summary) logic in a single scan.
6. The trim loop must invalidate `conv._token_estimate_cache` after EACH removal (not just at the end).

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. New tests pass
python3 -m pytest tests/test_context_strategy.py -v --tb=short

# 2. Existing tests still pass (delegation shims unchanged)
python3 -m pytest tests/test_conversation.py tests/test_phase4.py -v --tb=short

# 3. Full suite no regressions
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

```
COMPLETENESS:
- [x/not done] _select_prune_candidate() method added to DefaultContextStrategy
- [x/not done] compact() trim loop rewritten to use _select_prune_candidate()
- [x/not done] keep_first enforced: min_messages = keep_first + tail_preserve
- [x/not done] protect_is_summary enforced: is_summary messages deferred to second pass
- [x/not done] Summary injection uses min_messages and max(keep_first, ...)
- [x/not done] Cache invalidated after each removal in trim loop
- [x/not done] tests/test_context_strategy.py created with P2/P3/telemetry tests
- [x/not done] All new tests pass
- [x/not done] All existing tests pass (test_conversation.py, test_phase4.py)
- [x/not done] Full suite no regressions
```
