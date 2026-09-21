# CONTEXT BLOAT — Phase 2 Instructions (Trim Algorithm Fix + System Prompt Budget)

**For:** QTR (builder)
**From:** Qaster (implementation supervisor)
**Date:** 2026-06-17
**Spec (authoritative contract):** `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-2.md`
**Source bug report:** `docs/bugs/BUG-high-input-token-context-bloat.md` → BUG #2 (CRITICAL)
**Source proposal:** `docs/proposals/PROPOSAL-context-bloat-fix.md` → §5 Phase CB-2
**Target branch:** main
**Depends on:** CB-1 must be merged (commit `601067b`; adds `_compute_model_max` and the per-iteration trim call). The CB-1 implementation is currently in the working tree awaiting separate approval.

---

## What you're building

**Two independent fixes, one phase:**

1. **Trim algorithm fix** (1-line algorithm change in `models/conversation.py`). The current fallback at line 292 scans `range(1, len-1)` for USER messages and stalls at 21+ messages when the middle of the conversation is all ASSISTANT. The fix is to pop the oldest message in the trimmable region (index 0) regardless of role. Empirically verified by the supervisor: 40 alternating USER/ASSISTANT messages with `max_tokens=500` currently trim to 21 msgs / 2102 tokens (4x over budget). With the fix, they trim to 4 msgs / 404 tokens (under budget). All existing trim tests continue to pass.

2. **System prompt budget** (~90 lines new code in `utils/prompt_loader.py` + 60 lines in `agent/context.py` + 12 lines in `agent/runtime.py`). `compose_system_prompt()` and `build_system_prompt()` get a new optional `model_max_tokens` kwarg. When provided, the system prompt is budgeted to 15% of the model context window (16K hard cap fallback). File context is truncated to fit, but core files (README, AGENTS, CONVENTIONS, ARCHITECTURE) are always preserved at the end of the file context. The runtime at `create_conversation()` plumbs the default provider's `max_tokens` through.

**Result:** After CB-2, the trim actually reaches its target budget (not 21+ messages), and the system prompt stops bloating the input token count for projects with large file context.

---

## The spec is the contract

**Read `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-2.md` in full before writing any code.** All 12 sections. The spec was written with rule-level verification against the actual source (every code sample, every signature, every line number was checked). The trim fix was empirically tested and the system prompt budget was traced through the call chain. Do not re-derive the design — implement what the spec says.

**If you find a contradiction between this instructions file and the spec, the spec wins.** Flag the contradiction in the COMPLETENESS checklist and ask me to clarify before implementing.

---

## File scope (everything you will touch)

| File | Change type | Spec section |
|---|---|---|
| `models/conversation.py` | Replace trim fallback at line 292 with `pop(0)` (the oldest message in the trimmable region) | §2.1 |
| `utils/prompt_loader.py` | Add `model_max_tokens` kwarg to `compose_system_prompt()`; add `_apply_system_prompt_budget` and `_truncate_file_context_smart` helpers | §2.2 |
| `agent/context.py` | Add `CORE_FILES` constant and `build_file_context_with_core_files()` function; add `model_max_tokens` kwarg to `build_system_prompt()`; pass it through to `compose_system_prompt()` | §2.3, §2.4 |
| `agent/runtime.py` | At `create_conversation()` (line 1018), resolve the default provider's `max_tokens` and pass it to `build_system_prompt()` | §2.5 |
| `tests/test_conversation.py` | Add `TestTrimFallbackIncludesOldest` class (3 tests) | §2.6 |
| `tests/test_prompt_loader.py` | Add `TestSystemPromptBudget` class (4 tests) | §2.7 |
| `tests/test_context.py` | Add `TestBuildSystemPromptBudget` class (2 tests) | §2.8 |
| `docs/ARCHITECTURE.md` | Add core-files note to §4.4a; add new §4.4b "System Prompt Budget" | §2.9 |

**Files you will NOT touch (already correct or out of scope):**

- `prompts/system/*.md` — the prompt templates themselves. Out of scope.
- `utils/project_awareness.py:build_awareness_dict` — Phase CB-3 (BUG #6). Out of scope.
- `agent/runtime.py:_run_loop` — no changes. The system prompt is built once at conversation creation.
- `agent/runtime.py:_check_stuck` — Phase CB-3 (BUG #4). Out of scope.
- `agent/runtime.py:_call_llm_streaming` — Phase CB-3 (BUG #3). Out of scope.
- `models/conversation.py:get_token_estimate` — Phase CB-4 (BUG #5). Out of scope.
- `tests/test_conversation.py:TestConversationTrim` (4 tests at line 249) — existing tests, no changes.
- `tests/test_phase4.py:TestTrimSummaryInjection` (8 tests at line 280) — existing tests, no changes.
- `tests/test_prompt_loader.py:TestComposeSystemPrompt` (15+ tests at line 56) — existing tests, no changes (the new `model_max_tokens` kwarg is optional with `None` default).
- `agent/context.py:_read_key_files` (line 194) — read but not modified. `build_file_context` continues to call it; `build_file_context_with_core_files` adds the core files at the end (some duplication with the "Key files" section is intentional per spec §2.3).

---

## Implementation order (12 steps from spec §5)

Implement in this order. Verify at each step before moving on. **Do not batch.**

1. **Fix the trim fallback in `models/conversation.py`** — replace lines 295-302 (the `if not removed:` block) with the new fallback that pops index 0. Exact replacement is in spec §2.1.
   - **Verify:** `grep -n "range(1, len(self.messages) - 1)" models/conversation.py` → no matches.
   - **Verify:** `grep -n "self.messages.pop(0)" models/conversation.py` → at least one match.
   - **Verify:** `pytest tests/test_conversation.py -k "trim" -v` → all 4 existing `TestConversationTrim` tests still pass (backward-compat).

2. **Write `TestTrimFallbackIncludesOldest` in `tests/test_conversation.py`** — 3 tests (spec §2.6):
   - `test_fallback_removes_oldest_when_middle_is_all_assistant` — the exact QTR scenario (40 alternating msgs, max_tokens=500, expects ≤ 5 messages remaining, not 21).
   - `test_fallback_still_protects_preserved_tail` — the last 4 messages are never removed, even when no USER is in the trimmable region.
   - `test_fallback_does_not_remove_most_recent` — the most recent message (index -1) is never removed by the fallback.
   - **Verify:** `pytest tests/test_conversation.py::TestTrimFallbackIncludesOldest -v` → all 3 pass.

3. **Add `CORE_FILES` and `build_file_context_with_core_files()` to `agent/context.py`** — place after `build_file_context` at line 240 (the function ends around line 311; place the new code after the function's `return` statement). The function appends core files at the end of the file context. Exact body is in spec §2.3.
   - **Verify:** `grep -n "build_file_context_with_core_files" agent/context.py` → at least 2 matches (definition + the plumbed call site in `compose_system_prompt`).
   - **Verify:** `python3 -c "from agent.context import build_file_context_with_core_files; print('ok')"` → no import errors.

4. **Add `_apply_system_prompt_budget` and `_truncate_file_context_smart` to `utils/prompt_loader.py`** — place at the bottom of the file (after `compose_system_prompt`). The `_apply_system_prompt_budget` function computes the budget and decides whether to truncate; `_truncate_file_context_smart` does the section-boundary-aware truncation. Exact bodies are in spec §2.2.
   - **Verify:** `grep -n "_apply_system_prompt_budget\|_truncate_file_context_smart" utils/prompt_loader.py` → at least 2 matches each.

5. **Add `model_max_tokens` kwarg to `compose_system_prompt()`** — add the new optional parameter (place at the end of the kwarg list to preserve positional/keyword semantics), update the docstring, and replace the file context append block (around lines 266-271) to use `_apply_system_prompt_budget`. Exact changes are in spec §2.2.
   - **Verify:** `grep -n "model_max_tokens" utils/prompt_loader.py` → at least 3 matches (signature, call site in file context block, docstring).

6. **Add `model_max_tokens` kwarg to `build_system_prompt()` in `agent/context.py`** — add the new optional parameter and pass it to `compose_system_prompt()`. Exact changes are in spec §2.4.
   - **Verify:** `grep -n "model_max_tokens" agent/context.py` → at least 2 matches (signature + call site).

7. **Update `agent/runtime.py:create_conversation()` at line 1018** — resolve the default provider's `max_tokens` from `self._config` and pass it to `build_system_prompt()`. The current call is `system_prompt = build_system_prompt(agent_name, project_path, tool_names, agent_role=agent_role)`. Replace with the 12-line block from spec §2.5.
   - **Verify:** `grep -n "model_max_tokens" agent/runtime.py` → at least 1 match (the new call site).
   - **Verify:** `python3 -c "from agent.runtime import AgentRuntime; print('ok')"` → no import errors.

8. **Write `TestSystemPromptBudget` in `tests/test_prompt_loader.py`** — 4 tests (spec §2.7):
   - `test_no_budget_when_model_max_is_none` — backward-compat: no truncation when `model_max_tokens=None`.
   - `test_budget_truncates_file_context_to_15_percent` — small `model_max` truncates the file context.
   - `test_hard_cap_when_model_max_is_zero` — when `model_max_tokens=0`, the 16K hard cap applies.
   - `test_core_files_preserved_at_end` — README and AGENTS are preserved even when the file context is truncated.
   - **Verify:** `pytest tests/test_prompt_loader.py::TestSystemPromptBudget -v` → all 4 pass.

9. **Write `TestBuildSystemPromptBudget` in `tests/test_context.py`** — 2 tests (spec §2.8):
   - `test_model_max_is_plumbed_through` — `build_system_prompt` with small `model_max` produces a shorter prompt than with large `model_max`.
   - `test_no_model_max_means_no_truncation` — when `model_max=None`, the file context is preserved.
   - **Verify:** `pytest tests/test_context.py::TestBuildSystemPromptBudget -v` → all 2 pass.

10. **Run the full test suite.**
    - **Verify:** `pytest tests/ -q` → all tests pass, no regressions.
    - **Verify:** The existing `TestConversationTrim` (4 tests), `TestTrimSummaryInjection` (8 tests), `TestComposeSystemPrompt` (15+ tests), and `TestBuildSystemPrompt` (12 tests) continue to pass without modification.

11. **Update `docs/ARCHITECTURE.md`** — append the core-files note to §4.4a (search for the existing §4.4a section by `grep -n "§4\.4a" docs/ARCHITECTURE.md` or `_read_crabcakes_docs`); add a new §4.4b section. Exact text is in spec §2.9.
    - **Verify:** `grep -n "§4.4b\|System Prompt Budget" docs/ARCHITECTURE.md` → 2 matches.

12. **Final self-audit before reporting back:**
    - All 4 production code anchors hit (trim fix, prompt_loader, context, runtime)
    - All 3 test classes added (9 new tests total)
    - Full test suite green (1625+ tests, 0 regressions)
    - Doc updated (§4.4a note + new §4.4b section)
    - No collateral edits (only the lines specified above changed)
    - **No new public API surface** beyond the one optional `model_max_tokens` kwarg on `compose_system_prompt()` and `build_system_prompt()`.

---

## Hard rules (from `prompts/steelFramedCodeWriter.md`)

You MUST follow these. Violating any one is grounds for me to send the work back.

- **Rule 1 (Read Before You Write):** Read every file you will touch in full. Output a discovery block before writing any code.
- **Rule 2 (Hard Part First):** Start with the trim fix (step 1) — it's the most algorithmically interesting and the test for it is the most discriminating. Verify it works BEFORE moving to the system prompt budget.
- **Rule 3 (Verify Every Claim):** After the trim fix, run `python3 -c "from models.conversation import Conversation; c = Conversation(); ..."` to actually exercise the fix. Don't trust your mental model.
- **Rule 4 (Every Test Must Be Able to Fail):** For each test, ask "would this pass if the feature were broken?" If yes, fix the test. The `TestTrimFallbackIncludesOldest::test_fallback_removes_oldest_when_middle_is_all_assistant` test must actually verify the trim reaches the 4-5 message floor — a test that just checks `trimmed_this_turn` is True is a helper test, not a behavior test.
- **Rule 5 (Wire It Up or Delete It):** The `_apply_system_prompt_budget` and `_truncate_file_context_smart` helpers MUST be called from `compose_system_prompt()`. If you find yourself defining them but not calling them, delete them.
- **Rule 5a (Setter-Emitter Pairing):** N/A for this work — no new `set_on_X` setters.
- **Rule 7 (Error Handling):** The system prompt budget helpers must handle edge cases: `model_max_tokens=None`, `model_max_tokens=0`, file context empty, templates exceeding the budget (in which case templates are returned without truncation). See spec §7 for the full edge case table.
- **Rule 8 (Do Not Modify What You Were Not Asked To):** Do not reformat, do not "improve" comments, do not reorder imports in the production files. Run `git diff` and verify.
- **Step 6.5 (Test-removal-on-delete):** Not applicable for this work — we're adding new code, not deleting.
- **Step 6.6 (Context-reading requirement):** When you do the related-bug scan, read 3+ lines of surrounding context before flagging "duplicates." `build_file_context_with_core_files` intentionally appends README/AGENTS/ARCHITECTURE even though `build_file_context` already includes them via `_read_key_files`. This is NOT a duplicate — it's by design (see spec §2.3). Don't try to fix it.
- **Step 6.8 (Spec Drift Verification):** Specs that hardcode line numbers drift as files grow. If you find a line number in the spec that doesn't match the current file, use `grep -n` to find the real location. Flag the drift in the COMPLETENESS checklist.

---

## Adversarial audit (from `prompts/adversarialDebugger.md`)

Before reporting back, run the 11-section adversarial audit against your own changes. Common pitfalls to check for:

- **§1 (Challenge every assumption):** What if `model_max_tokens` is a negative number? The budget code checks `if model_max_tokens is not None and model_max_tokens > 0:` — verify this handles negatives correctly (it should fall through to the hard cap).
- **§2 (Trace the failure backwards):** The trim fix's failure mode (the one QTR found) was: 40 alternating messages stalled at 21. The new fix must reach the 4-5 message floor. Verify by running the test.
- **§4 (Test weakest links):** What if `project_path` is None? `build_file_context_with_core_files` returns "". `_apply_system_prompt_budget` returns the template result unchanged.
- **§5 (Error handling):** What if `re.split` raises (extremely unlikely but possible with malformed input)? The `_truncate_file_context_smart` function should be wrapped in try/except, or the split should be defensive.
- **§7 (Break the external contract):** What if a test passes `model_max_tokens` as a string ("200000")? The code does `int(model_max_tokens * 0.15) * 4` — if `model_max_tokens` is a string, the multiplication raises `TypeError`. Document the expected type (int) in the docstring.
- **§9 (Verify scope coverage):** All 8 files (4 production + 3 test + 1 doc) touched? `grep` for the new symbols in each.
- **§11 (Tests match the change):** The 9 new tests must exercise the new behaviors, not just the existence of the new functions.

**If you find any bug while auditing**, add it to the COMPLETENESS checklist as "Related issue found — not fixed in this phase: [description]" per `steelFramedCodeWriter.md` Step 6.6.

---

## Required output format

After all 12 steps, report back with:

### 1. Files changed (with line numbers)
```
models/conversation.py
  - L<new> to L<new>: replaced trim fallback (range scan → pop(0))

utils/prompt_loader.py
  - L<signature>: added model_max_tokens kwarg to compose_system_prompt
  - L<new> to L<new>: added _apply_system_prompt_budget helper
  - L<new> to L<new>: added _truncate_file_context_smart helper
  - L<new> to L<new>: updated file context append block to use the budget

agent/context.py
  - L<new> to L<new>: added CORE_FILES constant and build_file_context_with_core_files
  - L<signature>: added model_max_tokens kwarg to build_system_prompt
  - L<call site>: plumbed model_max_tokens through to compose_system_prompt

agent/runtime.py
  - L<new> to L<new>: in create_conversation, resolve default provider's max_tokens and pass to build_system_prompt

tests/test_conversation.py
  - L<new> to L<new>: added TestTrimFallbackIncludesOldest class

tests/test_prompt_loader.py
  - L<new> to L<new>: added TestSystemPromptBudget class

tests/test_context.py
  - L<new> to L<new>: added TestBuildSystemPromptBudget class

docs/ARCHITECTURE.md
  - L<line>: appended core-files note to §4.4a
  - L<line>: added new §4.4b "System Prompt Budget" section
```

### 2. Verification outputs (paste the actual command output, not a summary)
```
$ pytest tests/test_conversation.py::TestTrimFallbackIncludesOldest -v
<paste full output, must show all 3 pass>

$ pytest tests/test_conversation.py -k "trim" -v
<paste full output, must show existing 4 tests still pass>

$ pytest tests/test_prompt_loader.py::TestSystemPromptBudget -v
<paste full output, must show all 4 pass>

$ pytest tests/test_context.py::TestBuildSystemPromptBudget -v
<paste full output, must show both pass>

$ pytest tests/ -q
<paste full output, must show all 1625+ tests pass with no regressions>

$ grep -n "range(1, len(self.messages) - 1)" models/conversation.py
<paste output, must show no matches>

$ grep -n "self.messages.pop(0)" models/conversation.py
<paste output, must show at least one match>

$ grep -n "_apply_system_prompt_budget\|_truncate_file_context_smart" utils/prompt_loader.py
<paste output, must show 2+ matches each>

$ grep -n "build_file_context_with_core_files" agent/context.py
<paste output, must show 2+ matches>

$ grep -n "model_max_tokens" agent/runtime.py
<paste output, must show at least 1 match>

$ grep -n "§4.4b" docs/ARCHITECTURE.md
<paste output, must show 1 match>
```

### 3. COMPLETENESS checklist (MANDATORY, exact format)
```
COMPLETENESS:
- [x] Step 1: trim fallback replaced (pop index 0) — evidence: <line, grep, pytest output>
- [x] Step 2: TestTrimFallbackIncludesOldest (3 tests) — evidence: <pytest output>
- [x] Step 3: CORE_FILES + build_file_context_with_core_files — evidence: <line, grep>
- [x] Step 4: _apply_system_prompt_budget + _truncate_file_context_smart — evidence: <line, grep>
- [x] Step 5: model_max_tokens kwarg in compose_system_prompt — evidence: <line, grep>
- [x] Step 6: model_max_tokens kwarg in build_system_prompt — evidence: <line, grep>
- [x] Step 7: agent/runtime.py create_conversation plumbs model_max — evidence: <line, grep>
- [x] Step 8: TestSystemPromptBudget (4 tests) — evidence: <pytest output>
- [x] Step 9: TestBuildSystemPromptBudget (2 tests) — evidence: <pytest output>
- [x] Step 10: full test suite green — evidence: <paste pytest output>
- [x] Step 11: ARCHITECTURE.md §4.4a note + §4.4b section — evidence: <grep>
- [x] Step 12: final self-audit clean — evidence: <one-sentence summary>

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
2. Run the 11 verification commands above myself and compare to your pasted output.
3. `git diff` to verify only the specified lines changed.
4. `grep -n` for all the new symbols to confirm placement.
5. Read the actual code in the diff, not your summary.
6. **Independently re-run the trim fix's scenario**: create a 40-message alternating conversation, call `trim_to_token_limit(500)`, verify the result is ≤ 5 messages (not 21).
7. Verify no collateral edits in the production files (no reformatting, no comment "improvements", no import reordering).
8. Verify your tests actually exercise the new behaviors (a test that only calls a helper is a helper test, not a behavior test — it would not catch a regression where the budget is unwired).

**If I find a bug, I'll send it back with a bug report in the `adversarialDebugger.md` BUG format.** I will not silently fix it myself unless it's a 1-2 line trivial (per implementationSupervisor.md §6).

---

## Word marker

**"please write"** — this is the standing-order word marker per `implementationSupervisor.md` §9.4. Include it in your reply to confirm canonical receipt.

---

## Quick reference: spec sections you'll need

- Spec §2.1: trim fallback replacement (exact code)
- Spec §2.2: compose_system_prompt signature + 2 new helpers (`_apply_system_prompt_budget`, `_truncate_file_context_smart`)
- Spec §2.3: `build_file_context_with_core_files` body + `CORE_FILES` constant
- Spec §2.4: `build_system_prompt` signature + plumbed call to `compose_system_prompt`
- Spec §2.5: `agent/runtime.py:create_conversation` 12-line block
- Spec §2.6: `TestTrimFallbackIncludesOldest` template (3 tests)
- Spec §2.7: `TestSystemPromptBudget` template (4 tests)
- Spec §2.8: `TestBuildSystemPromptBudget` template (2 tests)
- Spec §2.9: ARCHITECTURE.md §4.4a note + new §4.4b section text
- Spec §5: implementation order (12 steps) — you are here
- Spec §6: acceptance criteria (the 14-item checklist — every item must be ticked off)
- Spec §7: edge cases (17+ cases — your code handles all of them by following the spec literally)

**When in doubt, follow the spec literally.** The spec was written with rule-level verification. If the spec and your judgment disagree, the spec wins unless the spec is clearly wrong (in which case flag it and ask).

---

## Important: this is two sub-phases that don't share code paths

The trim fix (`models/conversation.py`) and the system prompt budget (`utils/prompt_loader.py` + `agent/context.py` + `agent/runtime.py`) are **completely independent**. They:

- Touch different files (the only shared file is `agent/context.py` if you count the docstring update, but even there the trim doesn't touch context.py).
- Have different test files (`tests/test_conversation.py` for the trim, `tests/test_prompt_loader.py` + `tests/test_context.py` for the budget).
- Have no data flow coupling (the trim runs at the start of each `_run_loop` iteration; the budget runs once at `create_conversation`).

You CAN implement and test them in any order. The spec's implementation order is just one option. If you prefer to do the budget first (because it's more code), that's fine — just update the COMPLETENESS checklist accordingly.

The supervisor will audit them as one phase but the work itself is parallelizable.

---

Proceed. I will be here when you report back.
