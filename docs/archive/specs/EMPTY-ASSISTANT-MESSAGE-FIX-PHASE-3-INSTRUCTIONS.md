# Phase 3 Instructions — Empty-Assistant-Message Fix (Regression Tests)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md`
**Phase:** 3 of 4
**File scope:** 1 file (`tests/test_conversation.py`)
**Estimated delta:** +5 new tests, ~50 lines

> **Reminder:** READ ALL FILES BEFORE STARTING. Before writing any code, output a discovery block per `steelFramedCodeWriter.md` Rule 1 + Step 0, trace the data flow per Step 0.5, then implement the hard part first per Rule 2.

---

## Context

Phase 1 added the **read-side filter** in `models/conversation.py:to_api_messages()`: when an empty assistant message (`content="" AND tool_calls=[]`) is encountered, substitute a placeholder string and emit a warning log.

Phase 2 added the **write-side guard** in `agent/runtime.py:2222`: when an LLM returns no choices and no content, persist a different descriptive placeholder instead of an empty string.

Phase 3 ships **regression tests** that lock in the new behavior so a future refactor cannot silently regress it. Tests live in `tests/test_conversation.py` because the read-side filter lives in `models/conversation.py`.

**Note:** Phase 3 tests ONLY cover the read-side filter (Phase 1). The write-side guard (Phase 2) is exercised at runtime in `agent/runtime.py`, which has its own test file and is out of scope for this phase per spec §1. If you want to add write-side tests, see "Out of scope" at the bottom.

---

## What to Change

### File: `tests/test_conversation.py` (currently 652 lines)

Add **5 new tests** to the `TestConversationToApiMessages` class (currently lines 166–222). The new tests go **immediately after `test_full_conversation_sequence`** (which ends at line 221), and **before** the `TestConversationTokenEstimate` class (starts at line 224).

The test framework is `pytest`. The file uses standard `assert` statements (no `unittest.TestCase`). The convention is: `c = Conversation(agent_name="Coder")`, manipulate, call `c.to_api_messages()`, assert on the result.

### Required new tests

#### Test 1: `test_empty_assistant_message_substitutes_placeholder`

When an assistant message has empty content AND empty tool_calls, the serialized form must substitute the read-side placeholder string.

```python
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
```

#### Test 2: `test_empty_assistant_message_logs_warning`

When the placeholder substitution fires, a WARNING-level log must be emitted with the index in the output list.

```python
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
```

#### Test 3: `test_assistant_message_with_only_tool_calls_does_not_substitute`

An assistant message with empty content BUT with tool_calls is NOT corrupt — strict providers accept this. The placeholder must NOT fire.

```python
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
```

#### Test 4: `test_assistant_message_with_only_content_does_not_substitute`

An assistant message with non-empty content AND empty tool_calls is the normal text-only response. The placeholder must NOT fire.

```python
    def test_assistant_message_with_only_content_does_not_substitute(self):
        """Non-empty content + no tool_calls is the normal text-only path; placeholder must NOT fire."""
        c = Conversation(agent_name="Coder")
        c.add_assistant_message("Here is the answer", [])
        msgs = c.to_api_messages()
        assert msgs == [{"role": "assistant", "content": "Here is the answer"}]
```

#### Test 5: `test_corrupt_message_mid_sequence_uses_correct_index_in_warning`

When a corrupt message appears in the middle of a sequence (not at index 0), the warning log must report the **output-list index** of the corrupt message, not the input-list index. This locks in the `len(result)` pattern in the warning.

```python
    def test_corrupt_message_mid_sequence_uses_correct_index_in_warning(self, caplog):
        """Corrupt message at non-zero position: warning reports output-list index, not input-list index.

        Sequence: [user('hello'), corrupt assistant, user('still there?')]
        Output list indices: 0='hello', 1=corrupt placeholder, 2='still there?'
        So the warning must report idx=1 (output list position), not idx=1 (input list position).

        In this case both happen to be 1, so we use a longer sequence to disambiguate:
        [user('a'), user('b'), corrupt assistant, user('c')]
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
```

### Final ordering in `TestConversationToApiMessages`

After your changes, the class will be (in order):

1. `test_empty_conversation_returns_nothing` (existing)
2. `test_system_prompt_becomes_first_system_message` (existing)
3. `test_user_message_format` (existing)
4. `test_assistant_message_with_text` (existing)
5. `test_assistant_message_with_tool_calls` (existing)
6. `test_tool_result_format` (existing)
7. `test_full_conversation_sequence` (existing)
8. **`test_empty_assistant_message_substitutes_placeholder` (NEW)**
9. **`test_empty_assistant_message_logs_warning` (NEW)**
10. **`test_assistant_message_with_only_tool_calls_does_not_substitute` (NEW)**
11. **`test_assistant_message_with_only_content_does_not_substitute` (NEW)**
12. **`test_corrupt_message_mid_sequence_uses_correct_index_in_warning` (NEW)**

---

## What NOT to Change

- **Do not** modify any existing test in `TestConversationToApiMessages` or any other class.
- **Do not** modify `models/conversation.py` (Phase 1 already shipped; tests only verify it).
- **Do not** modify `agent/runtime.py` (Phase 2 already shipped; tests for that path are out of scope).
- **Do not** add tests for the write-side guard (Phase 2 path). That requires constructing a fake AgentRuntime and is much heavier; spec §1 explicitly defers it. Out of scope.
- **Do not** add new test files. All new tests go in `tests/test_conversation.py`.
- **Do not** reformat, reorder, or "improve" existing tests (`steelFramedCodeWriter.md` Rule 8).
- **Do not** import new modules at the top of the file (use inline imports like the existing tests in `test_context_strategy_audit_fixes2.py` do — see example: `import logging` inside the test function).

---

## Verification Commands (run all, paste full output)

### V1. Confirm all 5 new tests are present

```
cd /path/to/projects/crabcakes && grep -n "def test_empty_assistant_message_substitutes_placeholder\|def test_empty_assistant_message_logs_warning\|def test_assistant_message_with_only_tool_calls_does_not_substitute\|def test_assistant_message_with_only_content_does_not_substitute\|def test_corrupt_message_mid_sequence_uses_correct_index_in_warning" tests/test_conversation.py
```

Expected: 5 lines returned, all in `TestConversationToApiMessages` class (between line 166 and ~285).

### V2. Run the new tests in isolation — they must pass

```
cd /path/to/projects/crabcakes && pytest tests/test_conversation.py::TestConversationToApiMessages -v 2>&1 | tail -25
```

Expected: 12 tests pass (7 existing + 5 new). 0 failures.

### V3. Run the entire test_conversation.py file — nothing breaks

```
cd /path/to/projects/crabcakes && pytest tests/test_conversation.py -v 2>&1 | tail -20
```

Expected: 65 tests pass (60 existing + 5 new). 0 failures.

### V4. Confirm existing tests are unchanged

```
cd /path/to/projects/crabcakes && git diff HEAD tests/test_conversation.py | grep "^-" | grep -v "^---"
```

Expected: only `+` lines (additions); no `-` lines (no deletions or modifications of existing tests).

### V5. Confirm new tests do not depend on anything outside the test's local scope except pytest + the imported modules

```
cd /path/to/projects/crabcakes && grep -n "^import\|^from" tests/test_conversation.py | head -20
```

Expected: the imports at the top of the file are unchanged. New imports (`import logging`) are inside test functions.

### V6. Live simulation: confirm each new test would have FAILED before Phase 1

For each of the 5 new tests, mentally simulate running it against the PRE-PHASE-1 code (where `to_api_messages` ASSISTANT branch was just `entry = {"role":"assistant","content":msg.content}`):

- Test 1: `c.add_assistant_message("", [])` → would have produced `[{"role":"assistant","content":""}]`, NOT `[{"role":"assistant","content":"[assistant returned no content — placeholder]"}]`. Test would FAIL pre-Phase-1. ✅
- Test 2: no warning log would have been emitted. `caplog.records` would be empty. Test would FAIL pre-Phase-1. ✅
- Test 3: empty content + tool_calls path was unchanged in Phase 1, so this test would PASS pre-Phase-1 too. **This test is a regression guard, not a forward-progress test.** It locks in the "do NOT substitute when tool_calls is non-empty" invariant. ✅
- Test 4: text-only path was unchanged in Phase 1, so this test would PASS pre-Phase-1 too. **Same as Test 3 — regression guard.** ✅
- Test 5: same as Test 2 but with index check. Pre-Phase-1, no warning at all, so `caplog.records` empty. Test would FAIL pre-Phase-1. ✅

Write a one-line summary of this in your report.

### V7. Live simulation: confirm each new test PASSES against current code

```
cd /path/to/projects/crabcakes && pytest tests/test_conversation.py::TestConversationToApiMessages::test_empty_assistant_message_substitutes_placeholder tests/test_conversation.py::TestConversationToApiMessages::test_empty_assistant_message_logs_warning tests/test_conversation.py::TestConversationToApiMessages::test_assistant_message_with_only_tool_calls_does_not_substitute tests/test_conversation.py::TestConversationToApiMessages::test_assistant_message_with_only_content_does_not_substitute tests/test_conversation.py::TestConversationToApiMessages::test_corrupt_message_mid_sequence_uses_correct_index_in_warning -v 2>&1 | tail -10
```

Expected: 5 passed.

---

## Related-Bug Scan (Step 6.6)

Before reporting done, scan `tests/test_conversation.py` for **other** test patterns that might already cover the new behavior or might be affected by Phase 1:

1. Are there any existing tests in `TestConversationToApiMessages` that test `add_assistant_message("", [])` and check the serialized form? (Yes — `test_assistant_message_with_tool_calls` at line 188. But it has tool_calls, so the placeholder doesn't fire. Confirm unchanged.)
2. Are there any tests elsewhere (not just in `tests/test_conversation.py`) that mock `to_api_messages` and might be affected by the new placeholder string? Search the project:
   ```
   cd /path/to/projects/crabcakes && grep -rln "to_api_messages" tests/ | head -10
   ```
3. Are there integration tests in `tests/test_agent_*.py` that hit the empty-content code path in `agent/runtime.py:2214` (the write-side guard)? If yes, those tests would now see the placeholder string in the saved conversation. They are out of scope for Phase 3 (spec §1) but flag them if you find any.

If you find any related issue, **do NOT silently fix it** — add it to the COMPLETENESS checklist as:

```
Related issue found — not fixed in this phase: <description>
```

---

## Out of Scope (per spec §1)

- **Write-side guard tests (Phase 2):** Testing `agent/runtime.py:2222` requires constructing a fake `AgentRuntime`, mocking the LLM provider, and intercepting `_auto_save`. This is heavy infrastructure. Spec §1 explicitly defers it to a separate phase if desired. Do NOT add write-side tests in Phase 3.
- **Mid-sequence corrupt-message tests beyond what's in Test 5:** The 5 tests above cover the core invariants. Additional permutations (e.g., 5 corrupt messages in a row, corrupt message at index 0, system prompt + corrupt) are nice-to-have but not required. If you want to add them, add them BELOW the 5 required tests and document them as "extra coverage" in the report.

---

## Reports Required

When complete, reply with:

1. **Discovery block** (DISCOVERY: list of files read + what you learned — note the existing `TestConversationToApiMessages` test pattern, the caplog usage pattern from `test_context_strategy_audit_fixes2.py`, the inline-import convention)
2. **Files changed** with line numbers (paste `git diff --stat`)
3. **Full output** of verification commands V1–V7
4. **COMPLETENESS checklist** (literal `**COMPLETENESS:**` block, mandatory)
5. **Related-bug scan** findings (Step 6.6)
6. **Implementation-choice rationale** (Step 6.7) — for Phase 3 the choices are:
   - (a) Why inline `import logging` inside test functions instead of at top of file? (Answer: matches existing project convention in `test_context_strategy_audit_fixes2.py` and `test_prompt_loader.py`. Avoids polluting module-level imports with test-only deps.)
   - (b) Why use `caplog.at_level(logging.WARNING, logger="models.conversation")` instead of just `caplog.at_level(logging.WARNING)`? (Answer: scopes the capture to the specific logger to avoid noise from other modules.)
   - (c) Why Test 3 uses `tc = ToolCall(call_id="call_abc", ...)` directly? (Answer: simplest form. The earlier draft had a confusing `if False else` guard rail that was for the instructions file only, not the actual test code.)
   - If you made other choices, document them.
7. **Spec drift check** (Step 6.8) — confirm the test class boundaries (line 166 start, line 222 end-of-sequence, line 224 start-of-next-class) match the actual current file. If they drifted, flag it.

**Do not declare done** unless ALL of V1–V7 pass and the COMPLETENESS block is present.

---

## End of Phase 3 instructions.

Phase 4 (post-mortem + spec correction + related-bug backlog) will be sent in a separate `/ask` delegation after Phase 3 audit returns clean.