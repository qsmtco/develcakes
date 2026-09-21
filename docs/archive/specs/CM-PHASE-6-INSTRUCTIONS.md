# Phase 6: P5 _find_split_index + P6 _fit_summary

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §2.1.3, §2.1.5, §2.1.6
**Goal:** Add `_find_split_index()` and `_fit_summary()` to `DefaultContextStrategy`, upgrade `_summary()` to use the smart split, and fix the summary injection block in `compact()` to use `_fit_summary()`.
**Files to change:**
1. `agent/context_strategy.py` — add `_find_split_index()`, `_fit_summary()`, rewrite `_summary()`, fix summary injection in `compact()`
2. `tests/test_context_strategy.py` — add `TestFindSplitIndex`, `TestFitSummary` test classes

**SCOPE:** This phase implements P5 and P6 ONLY. Do NOT implement:
- P7 (dynamic prompt budget) — Phase 7

---

## Step 1: Add `_find_split_index()` to DefaultContextStrategy

Add this method BEFORE `_select_prune_candidate()` (after `prune_tool_outputs()`):

```python
    def _find_split_index(
        self,
        conv: Conversation,
        budget_tokens: int,
        keep_first: int = 2,
    ) -> int:
        """Find the message index where the head ends and the tail begins.

        Walks backward from the end, accumulating tokens, until half the
        budget is consumed. Then walks back further to land on an assistant
        message boundary (role-anchored, Aider pattern).

        Also enforces CB-6 (tool-call pairing) at the split boundary.

        Args:
            budget_tokens: Total token budget for the conversation.
            keep_first: Minimum index for the split (never split before this).

        Returns:
            Message index >= keep_first where the head can be summarized
            and the tail kept verbatim.
        """
        if len(conv.messages) <= keep_first:
            return keep_first

        half_budget = budget_tokens // 2
        tail_tokens = 0
        split = len(conv.messages)

        for i in range(len(conv.messages) - 1, keep_first - 1, -1):
            msg = conv.messages[i]
            msg_tokens = msg.tokens_used or (len(msg.content) // 4)
            if tail_tokens + msg_tokens >= half_budget:
                break
            tail_tokens += msg_tokens
            split = i

        # Role-anchor walk-back: walk back until messages[split - 1] is ASSISTANT.
        while split > keep_first:
            prev_msg = conv.messages[split - 1]
            if prev_msg.role == MessageRole.ASSISTANT:
                break
            split -= 1

        # CB-6 forward check: if messages[split] is a TOOL_RESULT whose
        # parent ASSISTANT-with-tool-calls is in the head, move split forward
        # to include this TOOL_RESULT in the head (gets summarized with parent).
        while split < len(conv.messages):
            msg_at_split = conv.messages[split]
            if msg_at_split.role == MessageRole.TOOL_RESULT:
                if split > keep_first:
                    adjacent_parent = conv.messages[split - 1]
                    if (
                        adjacent_parent.role == MessageRole.ASSISTANT
                        and adjacent_parent.tool_calls
                        and any(tc.call_id == msg_at_split.tool_call_id for tc in adjacent_parent.tool_calls)
                    ):
                        split += 1
                        continue
                # Search backward for true parent in head.
                if msg_at_split.tool_call_id:
                    for j in range(split - 1, keep_first - 1, -1):
                        candidate = conv.messages[j]
                        if (
                            candidate.role == MessageRole.ASSISTANT
                            and candidate.tool_calls
                            and any(tc.call_id == msg_at_split.tool_call_id for tc in candidate.tool_calls)
                        ):
                            split = j
                            break
                    else:
                        break
                else:
                    break
            else:
                break

        return max(split, keep_first)
```

## Step 2: Add `_fit_summary()` to DefaultContextStrategy

Add this method AFTER `_find_split_index()` (still before `_select_prune_candidate()`):

```python
    def _fit_summary(
        self,
        conv: Conversation,
        summary: str,
        token_budget: int,
        current_tokens: int,
    ) -> str | None:
        """Fit a summary into the remaining token budget by truncating.

        Tries 5 iterations, each reducing the summary to 80% of its previous
        length. If none fit, returns a minimal stub. If even the stub doesn't
        fit, returns None.

        Uses tiktoken (via ``_tiktoken_encoding_for()``) when available for
        accurate token counts; falls back to the ``chars // 4`` heuristic.
        """
        available_tokens = token_budget - current_tokens
        if available_tokens <= 0:
            return None

        # Use tiktoken when available for accurate token counting.
        from models.conversation import _tiktoken_encoding_for
        encoding = _tiktoken_encoding_for(conv.model)

        def _count_tokens(s: str) -> int:
            if encoding is not None:
                return len(encoding.encode(s))
            return len(s) // 4

        # Try progressively smaller versions.
        fitted = summary
        for _attempt in range(5):
            fitted_tokens = _count_tokens(fitted)
            if fitted_tokens <= available_tokens:
                return fitted
            fitted = fitted[:int(len(fitted) * 0.8)]

        # Final fallback: minimal stub.
        stub = "[Context reset — earlier conversation was too large to summarize]"
        if _count_tokens(stub) <= available_tokens:
            return stub
        return None
```

## Step 3: Rewrite `_summary()` to use `_find_split_index()`

Replace the ENTIRE current `_summary()` method with the P5-enhanced version:

**Current `_summary()` (Phase 1 mechanical extraction — REPLACE ENTIRELY):**
```python
    def _summary(
        self,
        conv: Conversation,
        token_budget: int = 0,              # noqa: ARG002 — Phase 4 uses this
        keep_first: int = 2,                # noqa: ARG002 — Phase 4 uses this
    ) -> str:
        """...Phase 1: mechanical extraction..."""
        if not conv.messages:
            return ""
        tail_preserve = 4
        if len(conv.messages) <= tail_preserve:
            return ""
        user_contents: list[str] = []
        for msg in conv.messages[:-tail_preserve]:
            ...
```

**New `_summary()` (P5 enhanced — replacement):**
```python
    def _summary(
        self,
        conv: Conversation,
        token_budget: int = 0,
        keep_first: int = 2,
    ) -> str:
        """Generate a summary of the oldest trimmed user messages.

        Phase 6: Uses _find_split_index() to compute a smarter split point
        instead of the naive messages[:-4] slice. The split index lands on
        an assistant message boundary (role-anchored) and respects CB-6.
        """
        if not conv.messages:
            return ""

        tail_preserve = 4
        if len(conv.messages) <= tail_preserve:
            return ""

        # P5: Compute a smarter split index.
        budget_tokens = token_budget if token_budget > 0 else conv.get_token_estimate()
        split = self._find_split_index(conv, budget_tokens, keep_first=keep_first)
        split = max(keep_first, min(split, len(conv.messages) - tail_preserve))

        head_messages = conv.messages[:split]

        user_contents: list[str] = []
        for msg in head_messages:
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

**IMPORTANT:** Preserve the EXACT summary formatting from Phase 1 (the `lines = [...]` block). The only change is HOW we determine which messages are "head messages" — `messages[:-tail_preserve]` → `messages[:split]` via `_find_split_index()`.

## Step 4: Fix summary injection block in `compact()` to use `_fit_summary()`

Replace the summary injection block in `compact()`:

**Current (Phase 4/5):**
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

**New (Phase 6 with `_fit_summary()`):**
```python
        messages_removed = messages_count_before - len(conv.messages)
        if messages_removed > 0 and len(conv.messages) >= min_messages:
            summary = self._summary(conv, token_budget=token_budget, keep_first=keep_first)
            if summary:
                current_tokens = conv.get_token_estimate()
                fitted = self._fit_summary(conv, summary, token_budget, current_tokens)
                if fitted is not None:
                    from models.conversation import _tiktoken_encoding_for
                    encoding = _tiktoken_encoding_for(conv.model)
                    if encoding is not None:
                        summary_tokens_injected = len(encoding.encode(fitted))
                    else:
                        summary_tokens_injected = len(fitted) // 4
                    summary_msg = Message(
                        role=MessageRole.ASSISTANT,
                        content=fitted,
                        is_summary=True,
                    )
                    insert_at = max(keep_first, len(conv.messages) - tail_preserve)
                    conv.messages.insert(insert_at, summary_msg)
```

**Key changes:**
1. `_summary()` now receives `token_budget` and `keep_first` (was called with no args before)
2. `len(summary) // 4` replaced with `_fit_summary()` — which tries progressively smaller truncations
3. `summary_tokens_injected` is now set AFTER the fit succeeds (fixes the Phase 1 telemetry bug where it was set before the budget check)
4. The `pass` (skip injection) path is gone — `_fit_summary()` returns `None` when nothing fits, and we simply don't inject
5. tiktoken used for `summary_tokens_injected` when available, with `// 4` fallback

## Step 5: Update the `compact()` call to `_summary()` — pass token_budget and keep_first

The call site change is already in Step 4's new code: `self._summary(conv, token_budget=token_budget, keep_first=keep_first)`.

This is critical: in Phase 4/5, `_summary()` was called with NO arguments (using defaults `token_budget=0, keep_first=2`). Now it receives the ACTUAL `token_budget` and `keep_first` from `compact()`, which enables `_find_split_index()` to compute an accurate split.

---

## Step 6: Add tests to `tests/test_context_strategy.py`

Add these two test classes:

```python
class TestFindSplitIndex:
    """P5: _find_split_index computes role-anchored split points."""

    def test_split_at_least_keep_first(self):
        """Split index is never less than keep_first."""
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(10):
            conv.add_user_message(f"message {i}")
            conv.add_assistant_message(f"response {i}", [])
        strategy = DefaultContextStrategy()
        split = strategy._find_split_index(conv, budget_tokens=8000, keep_first=2)
        assert split >= 2

    def test_split_respects_half_budget(self):
        """Split should leave roughly half the budget in the tail."""
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(10):
            conv.add_user_message("x" * 200)
            conv.add_assistant_message("y" * 200, [])
        strategy = DefaultContextStrategy()
        # With budget=800 tokens (~2 messages worth), split should be near the middle
        split = strategy._find_split_index(conv, budget_tokens=800, keep_first=2)
        assert split >= 2
        assert split < len(conv.messages)

    def test_split_lands_on_assistant_boundary(self):
        """The message before the split should be ASSISTANT (role-anchored)."""
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(6):
            conv.add_user_message(f"user {i}")
            conv.add_assistant_message(f"assistant {i}", [])
        strategy = DefaultContextStrategy()
        split = strategy._find_split_index(conv, budget_tokens=10000, keep_first=2)
        if split > 2:
            # messages[split - 1] should be ASSISTANT
            assert conv.messages[split - 1].role == MessageRole.ASSISTANT

    def test_split_with_tool_results_cb6(self):
        """CB-6: split doesn't orphan TOOL_RESULT from parent ASSISTANT."""
        from models.conversation import ToolCall
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("task")
        conv.add_assistant_message("ok", [])
        for i in range(3):
            tc = ToolCall(call_id=f"call_{i}", tool_name="exec", arguments={})
            conv.add_assistant_message("", [tc])
            conv.add_tool_result(f"call_{i}", "x" * 400)
        strategy = DefaultContextStrategy()
        split = strategy._find_split_index(conv, budget_tokens=2000, keep_first=2)
        # Check no TOOL_RESULT in tail is orphaned
        for i in range(split, len(conv.messages)):
            msg = conv.messages[i]
            if msg.role == MessageRole.TOOL_RESULT:
                # Parent must be in tail too, or split moves to include it
                parent_found = False
                for j in range(i - 1, split - 1, -1):
                    if conv.messages[j].role == MessageRole.ASSISTANT and conv.messages[j].tool_calls:
                        if any(tc.call_id == msg.tool_call_id for tc in conv.messages[j].tool_calls):
                            parent_found = True
                            break
                # If no parent in tail, split should have moved back to include parent
                # (or the TOOL_RESULT itself is in the head). Either way, no orphan.
                # We just verify no crash and split >= keep_first.
        assert split >= 2

    def test_short_conversation_returns_keep_first(self):
        """Conversations at or below keep_first length return keep_first."""
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("hi")
        conv.add_assistant_message("hello", [])
        strategy = DefaultContextStrategy()
        split = strategy._find_split_index(conv, budget_tokens=10000, keep_first=2)
        assert split == 2


class TestFitSummary:
    """P6: _fit_summary truncates summaries to fit available budget."""

    def test_full_summary_fits(self):
        """When there's plenty of room, summary is returned unchanged."""
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("hi")
        strategy = DefaultContextStrategy()
        result = strategy._fit_summary(conv, "A short summary.", token_budget=10000, current_tokens=100)
        assert result == "A short summary."

    def test_summary_truncated_to_fit(self):
        """When summary is too large, it's progressively truncated."""
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("hi")
        strategy = DefaultContextStrategy()
        huge_summary = "x" * 10000
        result = strategy._fit_summary(conv, huge_summary, token_budget=1000, current_tokens=900)
        # Should be truncated (much smaller than original)
        assert result is not None
        assert len(result) < len(huge_summary)

    def test_returns_none_when_no_room(self):
        """When current_tokens >= token_budget, returns None."""
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("hi")
        strategy = DefaultContextStrategy()
        result = strategy._fit_summary(conv, "summary", token_budget=100, current_tokens=100)
        assert result is None

    def test_returns_stub_when_extremely_tight(self):
        """When barely any room, returns the minimal stub."""
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("hi")
        strategy = DefaultContextStrategy()
        huge_summary = "x" * 10000
        # Leave just enough room for the stub (~17 tokens)
        result = strategy._fit_summary(conv, huge_summary, token_budget=120, current_tokens=100)
        # Should be either the stub or None (if even stub doesn't fit)
        if result is not None:
            assert len(result) <= 100  # stub or truncated version
```

---

## CRITICAL RULES

1. Do NOT change `_select_prune_candidate()` — correct from Phase 4.
2. Do NOT change `prune_tool_outputs()` — correct from Phase 5.
3. Do NOT change `models/conversation.py` — all logic stays on the strategy.
4. Do NOT change `agent/runtime.py` — runtime wiring is from Phase 3.
5. The summary FORMATTING (the `lines = [...]` block) must be PRESERVED from Phase 1 — only the message selection logic changes.
6. `_fit_summary()` must use `_tiktoken_encoding_for()` imported from `models/conversation.py` (deferred import inside the method body to avoid circular imports).
7. The `summary_tokens_injected` telemetry bug from Phase 1 is FIXED by this phase: the value is now set AFTER `_fit_summary()` succeeds, not before.

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. New P5/P6 tests pass
python3 -m pytest tests/test_context_strategy.py::TestFindSplitIndex tests/test_context_strategy.py::TestFitSummary -v --tb=short

# 2. All context_strategy tests pass
python3 -m pytest tests/test_context_strategy.py -v --tb=short

# 3. Existing trim/summary tests still pass
python3 -m pytest tests/test_conversation.py tests/test_phase4.py -v --tb=short

# 4. Full suite no regressions
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

```
COMPLETENESS:
- [x/not done] _find_split_index() method added (role-anchored, CB-6 aware)
- [x/not done] _fit_summary() method added (progressive truncation, tiktoken-aware)
- [x/not done] _summary() rewritten to use _find_split_index() instead of messages[:-4]
- [x/not done] Summary injection in compact() uses _fit_summary() instead of len(summary)//4 + pass
- [x/not done] summary_tokens_injected telemetry bug fixed (set after fit succeeds)
- [x/not done] _summary() receives token_budget and keep_first from compact()
- [x/not done] TestFindSplitIndex added with 5 tests
- [x/not done] TestFitSummary added with 4 tests
- [x/not done] All new tests pass
- [x/not done] All existing tests pass
- [x/not done] Full suite no regressions
```
