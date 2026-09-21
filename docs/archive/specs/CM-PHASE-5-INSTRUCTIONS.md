# Phase 5: P4 prune_tool_outputs — Lossless Layer 1 Compaction

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §2.1.4, §2.2.2
**Goal:** Add `prune_tool_outputs()` to `DefaultContextStrategy` and integrate it as Layer 1 inside `compact()`, called BEFORE the Layer 2 trim loop.
**Files to change:**
1. `agent/context_strategy.py` — add `prune_tool_outputs()` method, integrate into `compact()` as Layer 1
2. `tests/test_context_strategy.py` — add `TestPruneToolOutputs` class with 6 tests

**SCOPE:** This phase implements P4 ONLY. Do NOT implement:
- P5 (_find_split_index) — Phase 6
- P6 (_fit_summary with tiktoken) — Phase 6
- P7 (dynamic prompt budget) — Phase 7

---

## Step 1: Add `prune_tool_outputs()` to DefaultContextStrategy

Add this method to `DefaultContextStrategy`, BEFORE `_select_prune_candidate()`:

```python
    def prune_tool_outputs(
        self,
        conv: Conversation,
        target_tokens: int,
        protect_turns: int = 2,
    ) -> int:
        """Stub old tool results to free token budget. Returns tokens freed.

        Cheap lossless Layer 1 compaction. Walks backward from the end,
        skipping the protect_turns most recent TOOL_RESULT messages. For
        each unprotected TOOL_RESULT, replaces content with a short stub:

          "[compacted — {tool_name} output, {N} chars removed]"

        Stops when get_token_estimate() <= target_tokens.
        Idempotent: detects already-stubbed messages by their
        "[compacted —" prefix and skips them.
        """
        tokens_before = conv.get_token_estimate()
        if tokens_before <= target_tokens:
            return 0

        # Find TOOL_RESULT indices, most-recent-first.
        tool_result_indices: list[int] = []
        for i in range(len(conv.messages) - 1, -1, -1):
            if conv.messages[i].role == MessageRole.TOOL_RESULT:
                tool_result_indices.append(i)

        # Skip the protect_turns most recent tool results.
        prunable = tool_result_indices[protect_turns:]

        for idx in prunable:
            if conv.get_token_estimate() <= target_tokens:
                break
            msg = conv.messages[idx]
            # Idempotence: skip already-stubbed messages.
            if msg.content.startswith("[compacted —"):
                continue
            # Find the tool name from the parent ASSISTANT message's tool_calls.
            tool_name = "tool"
            if idx > 0:
                parent = conv.messages[idx - 1]
                if parent.role == MessageRole.ASSISTANT and parent.tool_calls:
                    # Match by tool_call_id to find the specific tool name.
                    for tc in parent.tool_calls:
                        if tc.call_id == msg.tool_call_id:
                            tool_name = tc.tool_name
                            break
            original_len = len(msg.content)
            msg.content = f"[compacted — {tool_name} output, {original_len} chars removed]"
            msg.tokens_used = 0
            # CRITICAL: invalidate cache after each mutation.
            conv._token_estimate_cache = None

        conv._token_estimate_cache = None
        tokens_after = conv.get_token_estimate()
        return tokens_before - tokens_after
```

## Step 2: Integrate `prune_tool_outputs()` into `compact()` as Layer 1

The `compact()` method currently goes straight to the trim loop (Layer 2). We need to insert a Layer 1 call BEFORE the trim loop, but ONLY if the budget is exceeded.

**Current top of `compact()` body (after docstring):**
```python
        # Snapshot state for telemetry BEFORE any mutation.
        messages_count_before = len(conv.messages)
        tokens_before = conv.get_token_estimate()
        conv._token_estimate_cache = None
        summary_tokens_injected = 0

        # Phase 4: P2/P3-aware trim parameters. ...
        tail_preserve = 4
        min_messages = keep_first + tail_preserve
```

**Add Layer 1 AFTER `min_messages` definition, BEFORE the trim loop:**
```python
        # ── Layer 1: prune_tool_outputs (P4) ──────────────────────────────────
        # Cheap lossless compaction: stub old tool result content in-place.
        # Runs before the trim loop (Layer 2) to preserve conversation structure
        # while reducing token usage. Returns tokens freed; we just call it for
        # its side effect.
        self.prune_tool_outputs(conv, token_budget, protect_turns=2)
        # Re-snapshot tokens_after after Layer 1 for telemetry accuracy.
        tokens_after_layer1 = conv.get_token_estimate()
```

**Also update `tokens_before` for telemetry:** The `tokens_before` snapshot at the top already captures the pre-compact state. After Layer 1 runs, the trim loop (Layer 2) will further reduce tokens. The telemetry's `tokens_freed` should reflect the TOTAL reduction (Layer 1 + Layer 2). No change needed — `tokens_before` is already captured before Layer 1, and the telemetry block after the trim loop computes `tokens_after` from `conv.get_token_estimate()`. The `tokens_freed` field naturally captures both layers.

## Step 3: Update telemetry to record Layer 1 info

In the `CompactionEvent` at the bottom of `compact()`, the existing fields work correctly. The `tokens_before` was captured before Layer 1, `tokens_after` after Layer 2. `tokens_freed = tokens_before - tokens_after` captures both layers.

However, we should add `layer` information to indicate multi-layer compaction occurred. Update the `CompactionEvent` construction:

**Current (Phase 4):**
```python
        self._last_result = CompactionEvent(
            ...
            layer=2,  # P2/P3/P6 trim layer
            ...
        )
```

**New (Phase 5):**
```python
        # Determine which layer(s) fired for telemetry.
        layer = 0
        if tokens_after_layer1 < tokens_before:
            layer = 1  # Layer 1 (prune_tool_outputs) fired
        if len(conv.messages) < messages_count_before:
            layer = max(layer, 2)  # Layer 2 (trim loop) also fired
        if layer == 0:
            layer = 2  # default: no compaction occurred, but report as layer 2

        self._last_result = CompactionEvent(
            ...
            layer=layer,
            ...
        )
```

**NOTE:** If the CompactionEvent in Phase 4 used a hardcoded `layer=2`, replace it with the dynamic computation above. If the telemetry block's structure makes this awkward, keep `layer=2` and add a comment that Phase 5+ may compute this dynamically. The key deliverable is that `prune_tool_outputs()` runs before the trim loop.

---

## Step 4: Add TestPruneToolOutputs to tests/test_context_strategy.py

Add this class to the existing `tests/test_context_strategy.py`:

```python
class TestPruneToolOutputs:
    """P4: Backwards-walk tool output pruning."""

    def test_oldest_tool_results_stubbed_first(self):
        """Tool results are stubbed oldest-first."""
        from models.conversation import ToolCall
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(3):
            tc = ToolCall(call_id=f"call_{i}", tool_name="exec_command", arguments={"cmd": f"echo {i}"})
            conv.add_assistant_message("", [tc])
            conv.add_tool_result(f"call_{i}", "x" * 5000)
        strategy = DefaultContextStrategy()
        freed = strategy.prune_tool_outputs(conv, target_tokens=500, protect_turns=1)
        assert freed > 0
        # First two tool results should be stubbed.
        assert "[compacted —" in conv.messages[1].content
        assert "[compacted —" in conv.messages[3].content
        # Most recent tool result should be intact.
        assert "[compacted —" not in conv.messages[5].content

    def test_protected_recent_turns_untouched(self):
        """The protect_turns most recent tool results are never stubbed."""
        from models.conversation import ToolCall
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(5):
            tc = ToolCall(call_id=f"call_{i}", tool_name="read_file", arguments={"path": f"f{i}"})
            conv.add_assistant_message("", [tc])
            conv.add_tool_result(f"call_{i}", "x" * 5000)
        strategy = DefaultContextStrategy()
        strategy.prune_tool_outputs(conv, target_tokens=200, protect_turns=2)
        # Last 2 tool results should be intact (they are the last and third-to-last messages).
        assert "[compacted —" not in conv.messages[-1].content
        assert "[compacted —" not in conv.messages[-3].content

    def test_idempotence(self):
        """Running prune_tool_outputs twice is a no-op the second time."""
        from models.conversation import ToolCall
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(3):
            tc = ToolCall(call_id=f"call_{i}", tool_name="exec_command", arguments={"cmd": "ls"})
            conv.add_assistant_message("", [tc])
            conv.add_tool_result(f"call_{i}", "x" * 5000)
        strategy = DefaultContextStrategy()
        freed1 = strategy.prune_tool_outputs(conv, target_tokens=500, protect_turns=1)
        freed2 = strategy.prune_tool_outputs(conv, target_tokens=500, protect_turns=1)
        assert freed2 == 0, f"Second prune should be no-op, freed={freed2}"

    def test_cb6_pairing_preserved(self):
        """Tool result still references the correct tool_call_id after stubbing."""
        from models.conversation import ToolCall
        conv = Conversation(agent_name="test", model="test/x")
        tc = ToolCall(call_id="call_42", tool_name="exec_command", arguments={"cmd": "ls"})
        conv.add_assistant_message("", [tc])
        conv.add_tool_result("call_42", "x" * 5000)
        strategy = DefaultContextStrategy()
        strategy.prune_tool_outputs(conv, target_tokens=100, protect_turns=0)
        tool_result_msg = [m for m in conv.messages if m.role == MessageRole.TOOL_RESULT][0]
        assert tool_result_msg.tool_call_id == "call_42"
        assistant_msg = [m for m in conv.messages if m.role == MessageRole.ASSISTANT and m.tool_calls][0]
        assert assistant_msg.tool_calls[0].call_id == "call_42"

    def test_token_cache_invalidated(self):
        """_token_estimate_cache is None after pruning."""
        from models.conversation import ToolCall
        conv = Conversation(agent_name="test", model="test/x")
        for i in range(3):
            tc = ToolCall(call_id=f"call_{i}", tool_name="exec_command", arguments={"cmd": "ls"})
            conv.add_assistant_message("", [tc])
            conv.add_tool_result(f"call_{i}", "x" * 5000)
        conv.get_token_estimate()
        assert conv._token_estimate_cache is not None
        strategy = DefaultContextStrategy()
        strategy.prune_tool_outputs(conv, target_tokens=500, protect_turns=1)
        assert conv._token_estimate_cache is None

    def test_no_prune_when_under_target(self):
        """prune_tool_outputs is a no-op when already under target."""
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("hi")
        strategy = DefaultContextStrategy()
        freed = strategy.prune_tool_outputs(conv, target_tokens=10000)
        assert freed == 0
```

---

## CRITICAL RULES

1. Do NOT change `_select_prune_candidate()` — it's correct from Phase 4.
2. Do NOT change `_summary()` — still Phase 1's mechanical extraction.
3. Do NOT use tiktoken — Phase 6 changes the summary estimation.
4. Do NOT change `models/conversation.py` — all P4 logic lives on the strategy.
5. Do NOT change `agent/runtime.py` — the runtime already calls `self._context_strategy.compact(conv, soft_ceiling)` from Phase 3. The Layer 1 call happens INSIDE `compact()`, not in the runtime.
6. `prune_tool_outputs()` mutates `msg.content` IN PLACE — it does not add/remove messages from `conv.messages`.
7. Cache invalidation: set `conv._token_estimate_cache = None` after EACH `msg.content` mutation inside the loop, AND once more after the loop.

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. New P4 tests pass
python3 -m pytest tests/test_context_strategy.py::TestPruneToolOutputs -v --tb=short

# 2. All context_strategy tests pass (P2 + P3 + P4)
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
- [x/not done] prune_tool_outputs() method added to DefaultContextStrategy
- [x/not done] prune_tool_outputs() integrated into compact() as Layer 1 (before trim loop)
- [x/not done] Idempotence: "[compacted —" prefix detection prevents double-stubbing
- [x/not done] protect_turns: most recent N tool results skipped
- [x/not done] Tool name extraction from parent ASSISTANT's tool_calls via tool_call_id matching
- [x/not done] Cache invalidated after each content mutation
- [x/not done] CB-6 pairing preserved: tool_call_id and parent tool_calls unchanged
- [x/not done] TestPruneToolOutputs added with 6 tests
- [x/not done] All new tests pass
- [x/not done] All existing tests pass
- [x/not done] Full suite no regressions
```
