# Phase 9: CB-6 Hardening for `_find_split_index` + Silent Exception Cleanup

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §1.4 CB-6, Phase 7 related-issue notes
**Goal:** Fix the CB-6 edge case in `_find_split_index` where a parent ASSISTANT-with-tool-calls at index `< keep_first` leaves an orphan TOOL_RESULT in the tail, and replace remaining `except Exception: pass` blocks in `agent/runtime.py` with `logger.debug(...)`.
**Files to change:**
1. `agent/context_strategy.py` — CB-6 hardening in `_find_split_index()`
2. `agent/runtime.py` — replace 2 remaining `except Exception: pass` with `logger.debug(...)`
3. `tests/test_context_strategy.py` — add `TestFindSplitIndexCB6Hardening` (3 tests)

**SCOPE:** This phase implements CB-6 hardening + silent exception cleanup ONLY. Do NOT implement:
- P9 context-pressure UI observability (deferred)
- P9a/P9b hierarchical/structured summaries (phase 2 proposal)

---

## Step 1: CB-6 Hardening in `_find_split_index`

### The Bug

When the CB-6 forward check encounters a TOOL_RESULT at `split` whose parent ASSISTANT-with-tool-calls is at index `< keep_first`, the current code handles this in the `if split > keep_first:` branch — but only when the parent is adjacent (`split - 1`). If the parent is NOT adjacent (e.g., there are intermediate messages between the ASSISTANT and the TOOL_RESULT), the backward search at `for j in range(split - 1, keep_first - 1, -1)` only searches down to `keep_first`. If the parent is AT `keep_first - 1`, it's NOT found by the search (range stops at `keep_first - 1` but the parent is at `keep_first - 1`, which IS included — wait, let me re-check).

Actually, `range(split - 1, keep_first - 1, -1)` goes from `split - 1` down to `keep_first` (inclusive), because `range(start, stop, -1)` is exclusive of `stop`. So the range includes indices `split-1, split-2, ..., keep_first`. The parent at `keep_first - 1` is NOT included.

**The edge case:** If the parent ASSISTANT-with-tool-calls is at index `keep_first - 1` (in the protected head), the backward search cannot find it. The current code falls through to `break`, leaving the TOOL_RESULT at `split` in the tail — orphaned from its parent in the head. This is a CB-6 violation.

### The Fix

After the backward search fails to find the parent, check if the parent is in the keep_first region (index `< keep_first`). If so, we must INCLUDE this TOOL_RESULT in the head (increment `split` past it), because:
- The parent ASSISTANT is protected in the head
- The TOOL_RESULT cannot exist without its parent (CB-6)
- Therefore the TOOL_RESULT must also be in the head

**Current code** (`agent/context_strategy.py:385-405` approx):
```python
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
```

**New code** — add parent-in-keep_first check after the backward search's `else` (for loop exhausted without finding parent):
```python
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
                # Search backward for true parent in trimmable region.
                if msg_at_split.tool_call_id:
                    found_parent = False
                    for j in range(split - 1, keep_first - 1, -1):
                        candidate = conv.messages[j]
                        if (
                            candidate.role == MessageRole.ASSISTANT
                            and candidate.tool_calls
                            and any(tc.call_id == msg_at_split.tool_call_id for tc in candidate.tool_calls)
                        ):
                            split = j
                            found_parent = True
                            break
                    if found_parent:
                        continue
                    # Parent not found in trimmable region. Check keep_first region.
                    # If parent is in keep_first (protected head), the TOOL_RESULT
                    # must also be in the head to preserve CB-6 pairing.
                    for j in range(min(keep_first, len(conv.messages)) - 1, -1, -1):
                        candidate = conv.messages[j]
                        if (
                            candidate.role == MessageRole.ASSISTANT
                            and candidate.tool_calls
                            and any(tc.call_id == msg_at_split.tool_call_id for tc in candidate.tool_calls)
                        ):
                            split += 1  # include TOOL_RESULT in head
                            found_parent = True
                            break
                    if not found_parent:
                        break
                else:
                    break
            else:
                break
```

**Key change:** After the backward search in the trimmable region fails (for-else `else` branch was `break`), we now search the keep_first region for the parent. If found, `split += 1` includes the TOOL_RESULT in the head. If still not found, `break`.

**CRITICAL:** The `continue` at the end of the `found_parent` block in keep_first continues the outer `while` loop, which re-checks the new `split` position. This handles consecutive TOOL_RESULTs whose parents are all in the head.

---

## Step 2: Replace remaining `except Exception: pass` in `agent/runtime.py`

### Location 1: line ~308 (tool-call args JSON parsing)

**Current:**
```python
            except Exception:
                pass
```

**New:**
```python
            except Exception as e:
                logger.debug("Failed to parse tool-call args JSON: %s", e)
```

### Location 2: line ~1350 (MCP cleanup)

**Current:**
```python
            except Exception:
                pass
```

**New:**
```python
            except Exception as e:
                logger.debug("MCP best-effort cleanup failed: %s", e)
```

**CRITICAL:** Find the EXACT lines with `grep -n "except Exception:" agent/runtime.py` first. There should be exactly 2 remaining (after Phase 7 fixed the third). Replace ONLY the `except Exception: pass` patterns — do not change any `except Exception as e:` patterns that already log.

---

## Step 3: Tests for CB-6 Hardening

Add to `tests/test_context_strategy.py`:

```python
class TestFindSplitIndexCB6Hardening:
    """CB-6 edge case: parent ASSISTANT-with-tool-calls in keep_first region."""

    def test_tool_result_orphan_included_in_head(self):
        """TOOL_RESULT whose parent is at keep_first-1 is included in head, not tail."""
        strategy = DefaultContextStrategy()
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        # keep_first=2: messages[0] and messages[1] are protected.
        # messages[0] = system-ish, messages[1] = ASSISTANT with tool_calls (parent)
        # messages[2] = TOOL_RESULT (child of messages[1])
        # messages[3+] = regular messages
        from models.conversation import Message, MessageRole
        conv.messages = []
        conv.add_user_message("question")
        # Parent ASSISTANT with tool_calls at index 1 (keep_first - 1)
        parent = Message(
            role=MessageRole.ASSISTANT,
            content="I'll check that",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
        )
        conv.messages.append(parent)
        # TOOL_RESULT at index 2 (would be at split)
        child = Message(
            role=MessageRole.TOOL_RESULT,
            content="result data",
            tool_call_id="call_1",
        )
        conv.messages.append(child)
        # Regular messages to fill the tail
        for i in range(10):
            conv.add_user_message(f"message {i} " + "x" * 1000)
            conv.add_assistant_message(f"response {i} " + "y" * 1000, [])

        split = strategy._find_split_index(conv, budget_tokens=8000, keep_first=2)
        # The TOOL_RESULT at index 2 must be included in the head (split > 2)
        # because its parent is at index 1 (keep_first - 1, protected).
        assert split > 2, f"TOOL_RESULT at index 2 should be in head, split={split}"

    def test_consecutive_tool_results_with_parent_in_head(self):
        """Multiple TOOL_RESULTs whose parent is in keep_first are all in head."""
        strategy = DefaultContextStrategy()
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        conv.messages = []
        conv.add_user_message("question")
        # Parent at index 1 with multiple tool_calls
        parent = Message(
            role=MessageRole.ASSISTANT,
            content="I'll check both",
            tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ],
        )
        conv.messages.append(parent)
        # Two TOOL_RESULTs at indices 2, 3
        conv.messages.append(Message(role=MessageRole.TOOL_RESULT, content="result1", tool_call_id="call_1"))
        conv.messages.append(Message(role=MessageRole.TOOL_RESULT, content="result2", tool_call_id="call_2"))
        # Regular messages
        for i in range(10):
            conv.add_user_message(f"msg {i} " + "x" * 1000)
            conv.add_assistant_message(f"resp {i} " + "y" * 1000, [])

        split = strategy._find_split_index(conv, budget_tokens=8000, keep_first=2)
        # Both TOOL_RESULTs must be in head
        assert split > 3, f"Both TOOL_RESULTs should be in head, split={split}"

    def test_no_orphan_when_parent_in_trimmable_region(self):
        """When parent is in trimmable region, normal CB-6 behavior applies (no change)."""
        strategy = DefaultContextStrategy()
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        conv.messages = []
        conv.add_user_message("question")
        conv.add_user_message("context")  # index 1 (filler for keep_first)
        # Parent at index 2 (in trimmable region when keep_first=2)
        parent = Message(
            role=MessageRole.ASSISTANT,
            content="checking",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
        )
        conv.messages.append(parent)
        conv.messages.append(Message(role=MessageRole.TOOL_RESULT, content="result", tool_call_id="call_1"))
        for i in range(10):
            conv.add_user_message(f"msg {i} " + "x" * 1000)
            conv.add_assistant_message(f"resp {i} " + "y" * 1000, [])

        split = strategy._find_split_index(conv, budget_tokens=8000, keep_first=2)
        # Normal behavior: split lands somewhere >= keep_first (2)
        assert split >= 2
```

---

## CRITICAL RULES

1. Do NOT change `models/conversation.py`.
2. Do NOT change `utils/prompt_loader.py`.
3. The CB-6 fix only adds a new search in the keep_first region — it does NOT change the existing backward search logic in the trimmable region.
4. The `except Exception: pass` replacements use `logger.debug(...)` with `%s` format strings (same pattern as Phase 7).
5. `logger` is already defined at module level in `agent/runtime.py`.
6. Do NOT change the `_find_split_index` signature.
7. Do NOT change the `_select_prune_candidate` method — that's Phase 4 code (CRITICAL RULE 1 from earlier phases).

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. New CB-6 hardening tests pass
python3 -m pytest tests/test_context_strategy.py::TestFindSplitIndexCB6Hardening -v --tb=short

# 2. All existing split index tests still pass
python3 -m pytest tests/test_context_strategy.py::TestFindSplitIndex -v --tb=short

# 3. All context_strategy tests pass
python3 -m pytest tests/test_context_strategy.py -v --tb=short

# 4. Runtime compaction tests pass (from Phase 8)
python3 -m pytest tests/test_runtime_compaction.py -v --tb=short

# 5. Full suite no regressions
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

```
COMPLETENESS:
- [ ] CB-6 fix: _find_split_index searches keep_first region for parent ASSISTANT
- [ ] CB-6 fix: orphan TOOL_RESULT included in head (split incremented past it)
- [ ] CB-6 fix: consecutive TOOL_RESULTs all handled
- [ ] except Exception: pass → logger.debug at line ~308 (tool-call args JSON)
- [ ] except Exception: pass → logger.debug at line ~1350 (MCP cleanup)
- [ ] TestFindSplitIndexCB6Hardening added with 3 tests
- [ ] All new tests pass
- [ ] All existing tests pass
- [ ] Full suite no regressions
```
