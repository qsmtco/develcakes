# CONTEXT BLOAT — Phase 3 Instructions (Streaming Usage, Stuck Messages, Awareness Caps)

**For:** QTR (builder)
**From:** Qaster (implementation supervisor)
**Date:** 2026-06-17
**Spec (authoritative contract):** `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-3.md`
**Source bug report:** `docs/bugs/BUG-high-input-token-context-bloat.md` → BUG #3 (HIGH), BUG #4 (HIGH), BUG #6 (MEDIUM)
**Source proposal:** `docs/proposals/PROPOSAL-context-bloat-fix.md` → §5 Phase CB-3
**Target branch:** main
**Depends on:** CB-1 (commit `601067b`) and CB-2 (commit `d43539e`) must be merged. Both are shipped.

---

## What you're building

**Three independent sub-fixes, one phase:**

1. **Streaming usage capture (BUG #3, HIGH).** The streamers at `agent/runtime.py:391-625` ignore the `usage` chunk that OpenAI-compatible providers emit at the end of every stream. `_call_llm_streaming` returns an empty usage dict, so `on_token_usage` fires with zero tokens for every streaming call (~50% of all calls). Fix: capture `usage` in the streamers, yield a new SSE event type `"usage"`, and populate the response's `usage` field in `_call_llm_streaming`.

2. **Stuck messages as transient prefix (BUG #4, HIGH).** The stuck detector's intervention message is appended to the tool result text and stored in `conv.messages`. For a stuck agent (10+ fires), that's 2,500+ chars of repetitive warning text in the conversation history. Fix: queue the intervention in a per-session `_pending_stuck_messages` list, prepend as a synthetic user message to the next LLM request only, then clear. Per proposal Q3 Option A.

3. **Awareness variable caps (BUG #6, MEDIUM).** `build_awareness_dict()` truncates `PROJECT_MEMORY` to 3,000 chars but `TEAM_ROSTER` and `CURRENT_STATE` have no caps. Fix: cap `TEAM_ROSTER` at 500 chars, `CURRENT_STATE` at 1,000 chars, with `[... truncated ...]` markers. Module-level constants.

**Result:** Streaming calls get accurate token counts (restores monitoring), stuck agents don't bloat history, awareness variables are bounded.

---

## The spec is the contract

**Read `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-3.md` in full before writing any code.** All 12 sections. The spec was written with rule-level verification against the actual source (every code sample, every signature, every line number was checked). Do not re-derive the design — implement what the spec says.

**If you find a contradiction between this instructions file and the spec, the spec wins.** Flag the contradiction in the COMPLETENESS checklist and ask me to clarify before implementing.

---

## File scope (everything you will touch)

| File | Change type | Spec section |
|---|---|---|
| `agent/runtime.py` | Capture usage in 3 streamers; handle `"usage"` event in `_call_llm_streaming`; init `_pending_stuck_messages`; consume pending in `_call_llm` and `_call_llm_streaming`; cleanup in `_cleanup_tool_history`; modify stuck injection at line 1428-1441 | §2.1, §2.2, §2.3 |
| `utils/project_awareness.py` | Add 2 module-level constants; truncate `TEAM_ROSTER` and `CURRENT_STATE` in `build_awareness_dict()` | §2.4 |
| `tests/test_agent_runtime.py` | Add `TestStreamingUsageCapture` (3 tests) and `TestStuckMessageTransient` (2 tests) | §2.5, §2.6 |
| `tests/test_project_awareness.py` | Add `TestAwarenessCaps` (2 tests) | §2.7 |
| `docs/ARCHITECTURE.md` | Update §7 `send_message` signature comment to include `[stuck-detection]` | §2.8 |

**Files you will NOT touch:**

- `prompts/system/*.md` — the prompt templates themselves. Out of scope.
- `utils/prompt_loader.py` — the awareness caps apply at the `build_awareness_dict` level; the system prompt composition is unchanged.
- `agent/runtime.py:_check_stuck` itself — unchanged. Only the CALLER (line 1428-1441) and the CONSUMER (`_call_llm`, `_call_llm_streaming`) change.
- `agent/runtime.py:_extract_usage` — unchanged. Already handles both `prompt_tokens`/`completion_tokens` (OpenAI format) and `input_tokens`/`output_tokens` (Anthropic format).
- `tests/test_agent_runtime.py:TestStuckDetection` (4 tests at line 1074) — existing tests, no changes.
- `tests/test_agent_runtime.py:TestStreaming` (existing tests at line 832+) — existing tests, no changes.
- `tests/test_project_awareness.py:TestBuildAwarenessBlock` (4 tests at line 129+) — existing tests, no changes.

---

## Implementation order (13 steps from spec §5)

Implement in this order. Verify at each step before moving on. **Do not batch.**

1. **Capture SSE usage in the three streamers (`agent/runtime.py:391-625`).** Add the `usage` capture blocks to `_stream_openai_events`, `_stream_minimax_events`, and `_stream_anthropic_events`. The OpenAI-format fix (in `_stream_openai_events` and `_stream_minimax_events`) is one block; the Anthropic-format fix is a different block. See spec §2.1.
   - **Verify:** `grep -n 'ev.type == "usage"' agent/runtime.py` → at least 3 matches (one per streamer).

2. **Handle the `"usage"` event in `_call_llm_streaming` (`agent/runtime.py:1605-1696`).** Add `captured_usage` accumulator alongside `full_content` and `tool_calls_partial`. Handle the new `"usage"` event. Return `captured_usage` in the success path AND the fallback path (replacing the hardcoded `usage: {}`).
   - **Verify:** `grep -n "captured_usage" agent/runtime.py` → at least 4 matches (init, event handler, success-path return, fallback-path return).

3. **Initialize `_pending_stuck_messages` in `AgentRuntime.__init__`** (alongside `_tool_history`).
   - **Verify:** `grep -n "_pending_stuck_messages" agent/runtime.py` → at least 5 matches (init, two consumption sites in `_call_llm`/`_call_llm_streaming`, the producer in `_run_loop`, the cleanup in `_cleanup_tool_history`).

4. **Modify the stuck injection at `agent/runtime.py:1428-1441`** to use the transient pattern. Replace the 3-line block `if stuck_msg: tool_result_text = ... + "\n\n---\n⚠️ " + stuck_msg` with a single line that appends to `self._pending_stuck_messages[session_key]`. See spec §2.3.

5. **Consume the pending stuck messages in `_call_llm` (line 1510)** and in `_call_llm_streaming` (line 1605). Add the same 8-line block at the top of each function (before any logger.debug calls). The block pops `_pending_stuck_messages[session_key]`, prepends a synthetic user message to the `messages` list, logs. See spec §2.3.

6. **Clean up pending stuck messages in `_cleanup_tool_history` (line 1748)** when the conversation ends. Add `self._pending_stuck_messages.pop(session_key, None)` after the existing tool-history cleanup.

7. **Write `TestStreamingUsageCapture` tests** (3 tests, see §2.5) in `tests/test_agent_runtime.py`. Use the existing `_mock_stream_openai_3_chunks` pattern as a template. The first test must verify that `on_token_usage` fires with non-zero tokens when a usage chunk is in the stream.
   - **Verify:** `pytest tests/test_agent_runtime.py::TestStreamingUsageCapture -v` → all 3 pass.

8. **Write `TestStuckMessageTransient` tests** (2 tests, see §2.6) in `tests/test_agent_runtime.py`. The first test must verify that `conv.messages` does NOT contain the stuck text after the loop. The second must verify that the next LLM call receives the stuck prefix.
   - **Verify:** `pytest tests/test_agent_runtime.py::TestStuckMessageTransient -v` → both pass.

9. **Add awareness caps to `utils/project_awareness.py:build_awareness_dict`** (modify `TEAM_ROSTER` and `CURRENT_STATE` blocks, add 2 module-level constants). See spec §2.4.
   - **Verify:** `grep -n "TEAM_ROSTER_MAX_CHARS\|CURRENT_STATE_MAX_CHARS" utils/project_awareness.py` → at least 4 matches (2 constant definitions + 2 usage sites).

10. **Write `TestAwarenessCaps` tests** (2 tests, see §2.7) in `tests/test_project_awareness.py`. The first test uses 30 team members (~1,500 chars) to force the cap. The second test forces `CURRENT_STATE` to exceed 1,000 chars.
    - **Verify:** `pytest tests/test_project_awareness.py::TestAwarenessCaps -v` → both pass.

11. **Run the full test suite.**
    - **Verify:** `pytest tests/ -q` → all tests pass, no regressions.
    - **Verify:** The existing `TestStuckDetection` (4 tests), `TestStreaming` (~5 tests), and `TestBuildAwarenessBlock` (4 tests) all continue to pass without modification.

12. **Update `docs/ARCHITECTURE.md`** — add `[stuck-detection]` to the `send_message` signature comment at §7 (find it with `grep -n "send_message(session_key" docs/ARCHITECTURE.md`). See spec §2.8.
    - **Verify:** `grep -n "stuck-detection" docs/ARCHITECTURE.md` → at least 1 match.

13. **Final self-audit before reporting back:**
    - All 4 production code anchors hit (3 streamers + _call_llm_streaming + stuck injection + 2 call sites + cleanup + 2 awareness caps)
    - All 7 new tests added and pass
    - Full test suite green (1634+ tests, 0 regressions)
    - Doc updated
    - No collateral edits (only the lines specified above changed)
    - **No new public API surface** beyond the one new private attribute (`_pending_stuck_messages`) and one new internal SSE event type (`"usage"`).

---

## Hard rules (from `prompts/steelFramedCodeWriter.md`)

You MUST follow these. Violating any one is grounds for me to send the work back.

- **Rule 1 (Read Before You Write):** Read every file you will touch in full. Output a discovery block before writing any code.
- **Rule 2 (Hard Part First):** Start with the streaming usage fix (steps 1-2) — it's the most algorithmically interesting and the test for it is the most discriminating. The fix is upstream (in the streamers) and downstream (in `_call_llm_streaming`) — both ends must be correct.
- **Rule 3 (Verify Every Claim):** After the streaming usage fix, run `python3 -c "from agent.runtime import _stream_openai_events, SSEEvent; ..."` to actually exercise the new event type. Don't trust your mental model.
- **Rule 4 (Every Test Must Be Able to Fail):** For each test, ask "would this pass if the feature were broken?" The first `TestStreamingUsageCapture` test must verify that `on_token_usage` fires with non-zero tokens — a test that just checks the response has a `usage` key (even if it's `{}`) is a false negative.
- **Rule 5 (Wire It Up or Delete It):** `_pending_stuck_messages` MUST be: initialized in `__init__`, populated in `_run_loop` (when stuck), consumed in `_call_llm` AND `_call_llm_streaming` (BOTH paths), cleaned up in `_cleanup_tool_history`. If you find yourself initializing it but never consuming it, delete the attribute.
- **Rule 5a (Setter-Emitter Pairing):** N/A for this work.
- **Rule 7 (Error Handling):** The streaming usage capture must be defensive: if `ev.data.get("usage")` is not a dict, treat as no usage. If the new `"usage"` event yields a malformed dict, the response's `usage` is `{}` (backward-compat).
- **Rule 8 (Do Not Modify What You Were Not Asked To):** Do not reformat, do not "improve" comments, do not reorder imports. Run `git diff` and verify.
- **Step 6.5 (Test-removal-on-delete):** Not applicable — we're adding new code, not deleting.
- **Step 6.6 (Context-reading requirement):** When the related-bug scan flags "duplicates," read 3+ lines of context. The new `usage` capture block in `_stream_openai_events` and `_stream_minimax_events` looks similar — that's because they share the OpenAI SSE format. Don't try to "fix" it.
- **Step 6.8 (Spec Drift Verification):** Specs that hardcode line numbers drift as files grow. The spec's line numbers (e.g., `_call_llm_streaming` at 1605-1696, stuck injection at 1428-1441) are anchor points. If the actual line numbers differ, use `grep -n` to find the real locations. Flag drift >10 lines in the COMPLETENESS checklist.

---

## Adversarial audit (from `prompts/adversarialDebugger.md`)

Before reporting back, run the 11-section adversarial audit against your own changes. Common pitfalls to check for:

- **§1 (Challenge every assumption):** What if `_check_stuck` returns `None` (not stuck)? No pending message queued; the next `_call_llm` doesn't prepend anything. Verify the `if pending:` guard.
- **§2 (Trace the failure backwards):** The streaming usage fix's failure mode is "the LLM emits a usage chunk but our streamer ignores it." Verify the new code path actually captures by running the test.
- **§4 (Test weakest links):** What if the LLM provider emits MULTIPLE usage chunks? The LAST one wins (overwrites the accumulator). Acceptable per spec.
- **§5 (Error handling):** What if `ev.data.get("usage")` is not a dict (e.g., a string)? `if usage:` is truthy, then `ev.data.get("usage", {})` returns the string. The response's `usage` would be a string, which would crash `_extract_usage`. Add a `isinstance(usage, dict)` check.
- **§7 (Break the external contract):** What if a test sets `rt._pending_stuck_messages[sk] = ["test"]` and then calls `_call_llm`? The pending message is prepended to the `messages` arg. The test should verify the FIRST message in the request is the stuck prefix.
- **§9 (Verify scope coverage):** All 5 files in scope touched? `grep` for the new symbols in each.
- **§11 (Tests match the change):** The 7 new tests must exercise the new behaviors, not just the existence of the new attributes.

**If you find any bug while auditing**, add it to the COMPLETENESS checklist as "Related issue found — not fixed in this phase: [description]" per `steelFramedCodeWriter.md` Step 6.6.

---

## Required output format

After all 13 steps, report back with:

### 1. Files changed (with line numbers)
```
agent/runtime.py
  - L<new> to L<new>: added 'usage' event capture in _stream_openai_events
  - L<new> to L<new>: added 'usage' event capture in _stream_minimax_events
  - L<new> to L<new>: added 'usage' event capture in _stream_anthropic_events
  - L<init>: added captured_usage accumulator in _call_llm_streaming
  - L<event handler>: added 'usage' event handling in _call_llm_streaming
  - L<returns>: replaced 'usage: {}' with 'usage: captured_usage' in success path
  - L<returns>: replaced 'usage: {}' with 'usage: captured_usage' in fallback path
  - L<init>: added self._pending_stuck_messages: dict[str, list[str]] = {}
  - L<new> to L<new>: prepended stuck messages in _call_llm (8-line block)
  - L<new> to L<new>: prepended stuck messages in _call_llm_streaming (8-line block)
  - L<stuck injection>: modified stuck injection at 1428-1441 to use transient pattern
  - L<cleanup>: added _pending_stuck_messages cleanup in _cleanup_tool_history

utils/project_awareness.py
  - L<new> to L<new>: added 2 module-level constants (TEAM_ROSTER_MAX_CHARS, CURRENT_STATE_MAX_CHARS)
  - L<TEAM_ROSTER block>: added 500-char cap with truncation marker
  - L<CURRENT_STATE block>: added 1000-char cap with truncation marker

tests/test_agent_runtime.py
  - L<new> to L<new>: added TestStreamingUsageCapture class (3 tests)
  - L<new> to L<new>: added TestStuckMessageTransient class (2 tests)

tests/test_project_awareness.py
  - L<new> to L<new>: added TestAwarenessCaps class (2 tests)

docs/ARCHITECTURE.md
  - L<line>: added [stuck-detection] to send_message signature comment
```

### 2. Verification outputs (paste the actual command output, not a summary)
```
$ pytest tests/test_agent_runtime.py::TestStreamingUsageCapture -v
<paste full output, must show all 3 pass>

$ pytest tests/test_agent_runtime.py::TestStuckMessageTransient -v
<paste full output, must show both pass>

$ pytest tests/test_project_awareness.py::TestAwarenessCaps -v
<paste full output, must show both pass>

$ pytest tests/test_agent_runtime.py -k "stuck or streaming" -v
<paste full output, must show ALL existing + new tests pass>

$ pytest tests/ -q
<paste full output, must show 1641+ tests pass with no regressions>

$ grep -n 'ev.type == "usage"' agent/runtime.py
<paste output, must show 3+ matches>

$ grep -n "captured_usage" agent/runtime.py
<paste output, must show 4+ matches>

$ grep -n "_pending_stuck_messages" agent/runtime.py
<paste output, must show 5+ matches>

$ grep -n "TEAM_ROSTER_MAX_CHARS\|CURRENT_STATE_MAX_CHARS" utils/project_awareness.py
<paste output, must show 4+ matches>

$ grep -n "stuck-detection" docs/ARCHITECTURE.md
<paste output, must show at least 1 match>
```

### 3. COMPLETENESS checklist (MANDATORY, exact format)
```
COMPLETENESS:
- [x] Step 1: usage capture in 3 streamers — evidence: <line, grep>
- [x] Step 2: usage event handling in _call_llm_streaming — evidence: <line, grep>
- [x] Step 3: _pending_stuck_messages init — evidence: <line, grep>
- [x] Step 4: stuck injection uses transient pattern — evidence: <line>
- [x] Step 5: stuck consumption in _call_llm + _call_llm_streaming — evidence: <line>
- [x] Step 6: stuck cleanup in _cleanup_tool_history — evidence: <line>
- [x] Step 7: TestStreamingUsageCapture (3 tests) — evidence: <pytest output>
- [x] Step 8: TestStuckMessageTransient (2 tests) — evidence: <pytest output>
- [x] Step 9: awareness caps in build_awareness_dict — evidence: <line, grep>
- [x] Step 10: TestAwarenessCaps (2 tests) — evidence: <pytest output>
- [x] Step 11: full test suite green — evidence: <paste pytest output>
- [x] Step 12: ARCHITECTURE.md [stuck-detection] — evidence: <grep>
- [x] Step 13: final self-audit clean — evidence: <one-sentence summary>

Related issues found (flagged, not fixed — per steelFramedCodeWriter Step 6.6):
- <none, or list each with one-sentence description>
```

**A response without the literal `**COMPLETENESS:** [x]` block is INCOMPLETE.** I will not accept the work without it. This is non-negotiable.

### 4. Implementation-choice rationale (for any non-obvious choice)
```
Rationale: <one sentence per non-obvious choice, citing the alternative rejected>
```

---

## What I will check (independent verification)

After you report, I will:

1. Load `prompts/adversarialDebugger.md` fresh and work through its 11 sections against your diff.
2. Run the 10 verification commands above myself and compare to your pasted output.
3. `git diff` to verify only the specified lines changed.
4. `grep -n` for all the new symbols to confirm placement.
5. Read the actual code in the diff, not your summary.
6. **Independently re-run the streaming usage scenario**: mock a streamer that emits a usage chunk, verify `_call_llm_streaming` returns the captured usage.
7. **Independently re-run the stuck scenario**: trigger `_check_stuck`, verify `conv.messages` does NOT contain the stuck text and the next `_call_llm` call has the prefix prepended.
8. **Independently re-run the awareness caps scenario**: 30 team members, verify `TEAM_ROSTER` is ≤ 500 + marker.
9. Verify no collateral edits in the production files.
10. Verify your tests actually exercise the new behaviors (a test that just constructs the helper is a helper test, not a behavior test).

**If I find a bug, I'll send it back with a bug report in the `adversarialDebugger.md` BUG format.** I will not silently fix it myself unless it's a 1-2 line trivial (per implementationSupervisor.md §6).

---

## Word marker

**"please write"** — this is the standing-order word marker per `implementationSupervisor.md` §9.4. Include it in your reply to confirm canonical receipt.

---

## Quick reference: spec sections you'll need

- Spec §2.1: streamer changes (3 functions, exact code blocks)
- Spec §2.2: `_call_llm_streaming` handler (init, event handler, returns)
- Spec §2.3: stuck message transient pattern (5 sub-steps: init, producer, 2 consumers, cleanup)
- Spec §2.4: awareness caps (constants + 2 truncations)
- Spec §2.5: `TestStreamingUsageCapture` template (3 tests)
- Spec §2.6: `TestStuckMessageTransient` template (2 tests)
- Spec §2.7: `TestAwarenessCaps` template (2 tests)
- Spec §2.8: ARCHITECTURE.md §7 `send_message` comment update
- Spec §5: implementation order (13 steps) — you are here
- Spec §6: acceptance criteria (14 items)
- Spec §7: edge cases (15+ cases)

**When in doubt, follow the spec literally.** The spec was written with rule-level verification. If the spec and your judgment disagree, the spec wins unless the spec is clearly wrong (in which case flag it and ask).

---

## Important: this is three sub-fixes that don't share code paths

The streaming usage fix (`agent/runtime.py:_stream_openai_events` + `_call_llm_streaming`), the stuck message fix (`agent/runtime.py:_run_loop` + `_call_llm` + `_call_llm_streaming` + `_cleanup_tool_history`), and the awareness caps (`utils/project_awareness.py:build_awareness_dict`) are **completely independent**. They:

- Touch different functions (except the stuck message fix touches `_call_llm` and `_call_llm_streaming`, which the streaming fix also touches).
- Have different test files (`tests/test_agent_runtime.py` for streaming + stuck, `tests/test_project_awareness.py` for caps).
- Have no data flow coupling.

You CAN implement and test them in any order. The spec's implementation order is one option. If you prefer to do the awareness caps first (because it's isolated), that's fine — just update the COMPLETENESS checklist accordingly.

The supervisor will audit them as one phase but the work itself is parallelizable.

---

Proceed. I will be here when you report back.
