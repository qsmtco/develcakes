# CONTEXT BLOAT — Phase 1 Instructions (BUG #1: wire up `trim_to_token_limit`)

**For:** QTR (builder)
**From:** Qaster (implementation supervisor)
**Date:** 2026-06-17
**Spec (authoritative contract):** `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-1.md`
**Source bug report:** `docs/bugs/BUG-high-input-token-context-bloat.md` → BUG #1 (CRITICAL)
**Source proposal:** `docs/proposals/PROPOSAL-context-bloat-fix.md` → §5 Phase CB-1
**Target branch:** main

---

## What you're building

**One bug fix, three observable effects:**

1. `Conversation.trim_to_token_limit()` is currently dead code — defined and unit-tested, never called by the runtime. You will call it once per `_run_loop` iteration, BEFORE the LLM call, so conversation history gets capped at the model's context window.
2. The throwaway `model_max` calculation that already exists at `agent/runtime.py:1198-1201` (used only for the §4.15 breakdown callback) gets hoisted into a private helper `_compute_model_max(conv)` that the trim call and the breakdown callback both use.
3. The §4.15 per-turn breakdown dict gets three new additive keys (`trimmed_this_turn`, `messages_remaining`, `messages_removed_this_turn`) so the UI can observe trimming through the existing observability channel — no new event type.

**Result:** OpenRouter's 106K–160K input-token bloat drops to <20K typical, <50K worst case (per the proposal's success criteria).

---

## The spec is the contract

**Read `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-1.md` in full before writing any code.** All 12 sections. The spec was written with rule-level verification against the actual source (every code sample, every signature, every line number was checked). Do not re-derive the design — implement what the spec says.

**If you find a contradiction between this instructions file and the spec, the spec wins.** Flag the contradiction in the COMPLETENESS checklist and ask me to clarify before implementing.

---

## File scope (everything you will touch)

| File | Change type | Spec section |
|---|---|---|
| `agent/runtime.py` | Add `_compute_model_max` method, add trim call block, replace breakdown block, add `_last_trim_removed` attr, update class docstring | §2.1, §2.2 |
| `tests/test_agent_runtime.py` | Add `TestRunLoopTrimsContext` and `TestComputeModelMax` classes (next to existing `TestToolLoop` at line 225) | §2.3, §2.4 |
| `docs/ARCHITECTURE.md` | Add three keys to §4.15 dict shape (find the section that documents the breakdown dict — search for "system_prompt_tokens" or "usage_percent") | §2.5, §8 |

**Files you will NOT touch (already correct, listed in spec §9):**

- `models/conversation.py` — `trim_to_token_limit()` and `get_token_breakdown()` are already correct. Do not edit.
- `utils/prompt_loader.py`, `agent/context.py` — Phase CB-2. Out of scope.
- `ui/handlers/agent_runtime_handler.py:935` — the breakdown consumer reads only its 6 known keys; additive new keys are safe. Do not edit.

---

## Implementation order (10 steps from spec §5)

Implement in this order. Verify at each step before moving on. **Do not batch.**

1. **Add `_compute_model_max` helper to `AgentRuntime`** — placement: directly above `_run_loop` (line 1106), or wherever matches the existing private-helper pattern. Exact body and exception-guard spec is in spec §2.1.
   - **Verify:** `grep -n "_compute_model_max" agent/runtime.py` → exactly one definition.

2. **Add `self._last_trim_removed = 0` to `AgentRuntime.__init__`** — place after the `_on_token_breakdown` line at line 904. This is the initial value; the attribute gets overwritten unconditionally on every iteration of `_run_loop`.
   - **Verify:** `grep -n "_last_trim_removed" agent/runtime.py` → three matches (init, trim write, post-dispatch reset).

3. **Add the trim call block to `_run_loop`** — insert between `messages = conv.to_api_messages()` (line 1144) and the `from agent.tools import get_tool_definitions_for_api` line. Exact block is in spec §2.1.
   - **Verify:** `grep -n "trim_to_token_limit" agent/runtime.py` → at least one new match in `_run_loop`.

4. **Replace the breakdown block at lines 1197-1203** with the enriched version (4 new lines + the post-dispatch reset). Exact replacement is in spec §2.1.
   - **Verify:** `grep -n "trimmed_this_turn" agent/runtime.py` → exactly one match.

5. **Update the `AgentRuntime` class docstring** — add the 3-key note after the `on_token_breakdown:` description around line 877. Exact text is in spec §2.2.
   - **Verify:** `grep -n "trimmed_this_turn" agent/runtime.py` → now two matches (docstring + breakdown assignment).

6. **Write `TestComputeModelMax` in `tests/test_agent_runtime.py`** — 5 tests (spec §2.4):
   - `test_returns_provider_max_tokens` — conv.model = "openrouter/some-model", provider cfg has max_tokens=200_000 → returns 200_000
   - `test_falls_back_to_128k_when_provider_unknown` — provider not in config → returns 128_000
   - `test_falls_back_to_128k_when_max_tokens_is_zero` — provider cfg max_tokens=0 → returns 128_000
   - `test_falls_back_to_128k_when_max_tokens_is_none` — provider cfg max_tokens=None → returns 128_000
   - `test_extracts_provider_name_from_slash_model` — conv.model = "openrouter/claude-3-opus" → extracts "openrouter" and uses its max_tokens
   - **Verify:** `pytest tests/test_agent_runtime.py::TestComputeModelMax -v` → all 5 pass.

7. **Write `TestRunLoopTrimsContext` in `tests/test_agent_runtime.py`** — 1 test (spec §2.3). Use the same pattern as the existing `TestToolLoop.test_user_plus_assistant_in_conversation` at line 226 of the test file. Build a config with `max_tokens=500` (tiny — forces the trim). Add 20 long exchanges to the conversation. Patch `rt._call_llm` with a lambda returning a text-only response (no tool calls → loop exits after one iteration). Capture the breakdown via `rt._on_token_breakdown = lambda sk, bd: captured.append(bd)`. Assert `len(conv.messages) < 20`, `trimmed_this_turn is True`, `messages_removed_this_turn > 0`.
   - **Verify:** `pytest tests/test_agent_runtime.py::TestRunLoopTrimsContext -v` → test passes.

8. **Run the full test suite.**
   - **Verify:** `pytest tests/test_agent_runtime.py tests/test_conversation.py tests/test_phase4.py -q` → all pass.
   - **Verify:** `pytest tests/ -q` → full suite passes, no regressions. Existing `TestConversationTrim` (4 tests at `tests/test_conversation.py:249`) and `TestTrimSummaryInjection` (8 tests at `tests/test_phase4.py:280`) continue to pass without modification.

9. **Update `docs/ARCHITECTURE.md`** — add three keys (`trimmed_this_turn`, `messages_remaining`, `messages_removed_this_turn`) to the §4.15 breakdown dict documentation. Find the existing dict shape by `grep -n "system_prompt_tokens\|usage_percent" docs/ARCHITECTURE.md`. Append the three new keys with a "(Phase CB-1)" suffix. Exact text is in spec §2.5.
   - **Verify:** `grep -n "trimmed_this_turn" docs/ARCHITECTURE.md` → one match.

10. **Final self-audit before reporting back:**
    - All 4 production code anchors hit (helper, init attr, trim call, breakdown block)
    - All 6 test cases pass (5 in `TestComputeModelMax`, 1 in `TestRunLoopTrimsContext`)
    - Full test suite green
    - Doc updated
    - No collateral edits (only the lines specified above changed)
    - **No new public API surface.** Only one new private method (`_compute_model_max`), one new private attribute (`_last_trim_removed`), and one new top-level call site for `trim_to_token_limit`. No new callbacks, no new event types, no new module-level constants.

---

## Hard rules (from `prompts/steelFramedCodeWriter.md`)

You MUST follow these. Violating any one is grounds for me to send the work back.

- **Rule 1 (Read Before You Write):** Read every file you will touch in full. Output a discovery block before writing any code (the format is in `prompts/steelFramedCodeWriter.md` Step 0).
- **Rule 2 (Hard Part First):** Start with `_compute_model_max` (step 1 above) — it's the most uncertain because of the exception guard. Verify it imports and the signature is correct BEFORE moving to the trim call.
- **Rule 3 (Verify Every Claim):** Run `inspect.signature()` on `_compute_model_max` after writing. Run `grep -n "_compute_model_max" agent/runtime.py` to confirm the definition landed in the right place. Do not trust your mental model.
- **Rule 4 (Every Test Must Be Able to Fail):** For each test, ask "would this pass if the feature were broken?" If yes, fix the test. The `TestRunLoopTrimsContext` test must actually exercise the trim call — a test that just constructs an `AgentRuntime` and exits without calling `_run_loop` is a helper test, not a behavior test.
- **Rule 5 (Wire It Up or Delete It):** `self._last_trim_removed` MUST be set in the trim block and read in the breakdown block within the same iteration. If you find yourself setting it in one place and reading it in a place that doesn't run, delete the attribute.
- **Rule 5a (Setter-Emitter Pairing):** N/A for this work — no new `set_on_X` setters. But: `_compute_model_max` is a new method. It MUST be called from somewhere. The two call sites are: the trim block (step 3) and the breakdown block (step 4). Confirm both.
- **Rule 7 (Error Handling):** `_compute_model_max` must catch any exception from `self._config.providers` lookups and return the 128_000 fallback. Do not let a malformed provider config crash the tool loop.
- **Rule 8 (Do Not Modify What You Were Not Asked To):** Do not reformat, do not "improve" comments, do not reorder imports in the production file. The only lines you should change are the ones this instructions file and the spec call out. Run `git diff` and verify.

---

## Adversarial audit (from `prompts/adversarialDebugger.md`)

Before reporting back, run the 11-section adversarial audit against your own changes. For each section, identify at least one probe and run it. Common pitfalls to check for:

- **§1 (Challenge every assumption):** What if `conv.model` is a string like `"openrouter"` with no `/`? What if `default_provider` is `None`? What if `self._config.providers` is a dict subclass with a `.get` that raises? (Your exception guard handles all of these — verify by reading your helper after writing it.)
- **§4 (Test weakest links):** What if the conversation has 0 messages when the trim block runs? What if the conversation has exactly 4 messages? What if `model_max = 0`? (The trim's `len(self.messages) > 4` guard handles the first two; the spec's `_compute_model_max` returns 128_000 for `max_tokens=0`.)
- **§5 (Error handling):** What if the trim raises? The runtime should NOT crash. The trim is wrapped in the existing `try/except` at the top of `_run_loop`? — verify by reading the surrounding context.
- **§7 (Break the external contract):** What if a test sets `rt._on_token_breakdown` to a callback that takes 0 args (the wrong signature)? Your breakdown dispatch in step 4 must still work — it uses `*args` via `_dispatch`.
- **§9 (Verify scope coverage):** All 3 files (runtime.py, test file, ARCHITECTURE.md) touched? `grep` for the new symbols in each.
- **§11 (Tests match the change):** Your `TestRunLoopTrimsContext` test must exercise the trim call, not just the existence of the `_compute_model_max` helper.

**If you find any bug while auditing**, add it to the COMPLETENESS checklist as "Related issue found — not fixed in this phase: [description]" per the `steelFramedCodeWriter.md` Step 6.6 rule. Do not silently fix it.

---

## Required output format

After all 10 steps, report back with:

### 1. Files changed (with line numbers)
```
agent/runtime.py
  - L<new> to L<new>: added _compute_model_max method
  - L904: added self._last_trim_removed = 0 in __init__
  - L<new> to L<new>: added trim call block in _run_loop
  - L1197-1203 (now L<new>-<new>): replaced breakdown block
  - L<docstring line>: added 3-key note to class docstring

tests/test_agent_runtime.py
  - L<new> to L<new>: added TestComputeModelMax class
  - L<new> to L<new>: added TestRunLoopTrimsContext class

docs/ARCHITECTURE.md
  - L<line>: added three new keys to §4.15 dict shape
```

### 2. Verification outputs (paste the actual command output, not a summary)
```
$ pytest tests/test_agent_runtime.py::TestComputeModelMax -v
<paste full output>

$ pytest tests/test_agent_runtime.py::TestRunLoopTrimsContext -v
<paste full output>

$ pytest tests/test_agent_runtime.py tests/test_conversation.py tests/test_phase4.py -q
<paste full output, must show all green>

$ pytest tests/ -q
<paste full output, must show no regressions>

$ grep -n "_compute_model_max\|_last_trim_removed\|trimmed_this_turn" agent/runtime.py
<paste output, must show all expected matches>

$ grep -n "trimmed_this_turn" docs/ARCHITECTURE.md
<paste output, must show one match>
```

### 3. COMPLETENESS checklist (MANDATORY, exact format)
```
COMPLETENESS:
- [x] Step 1: _compute_model_max added — evidence: <line, grep, signature>
- [x] Step 2: _last_trim_removed init — evidence: <line>
- [x] Step 3: trim call block in _run_loop — evidence: <line, grep>
- [x] Step 4: breakdown block replaced — evidence: <line, grep>
- [x] Step 5: class docstring updated — evidence: <line>
- [x] Step 6: TestComputeModelMax (5 tests) — evidence: <pytest output>
- [x] Step 7: TestRunLoopTrimsContext (1 test) — evidence: <pytest output>
- [x] Step 8: full test suite green — evidence: <paste pytest output>
- [x] Step 9: ARCHITECTURE.md §4.15 updated — evidence: <grep>
- [x] Step 10: final self-audit clean — evidence: <one-sentence summary>

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
2. Run the 6 pytest commands above myself and compare to your pasted output.
3. `git diff` to verify only the specified lines changed.
4. `grep -n` for all the new symbols to confirm placement.
5. Read the actual code in the diff, not your summary.
6. Verify no collateral edits in the production file (no reformatting, no comment "improvements", no import reordering).
7. Verify your test actually exercises the trim call (a test that doesn't call `_run_loop` is a helper test, not a behavior test — it would not catch a regression where the trim is unwired).

**If I find a bug, I'll send it back with a bug report in the `adversarialDebugger.md` BUG format.** I will not silently fix it myself unless it's a 1-2 line trivial (per implementationSupervisor.md §6).

---

## Word marker

**"please write"** — this is the standing-order word marker per `implementationSupervisor.md` §3 and `implementationLoop.md` §3.3. Include it in your reply to confirm canonical receipt.

---

## Quick reference: spec sections you'll need

- Spec §2.1: production file changes (helper, init attr, trim block, breakdown block, exact code)
- Spec §2.2: class docstring update
- Spec §2.3: TestRunLoopTrimsContext template (uses `tests/test_agent_runtime.py` — NOT `test_runtime.py`, that file does not exist)
- Spec §2.4: TestComputeModelMax template (5 tests)
- Spec §2.5: ARCHITECTURE.md §4.15 dict shape addition
- Spec §5: implementation order (10 steps) — you are here
- Spec §6: acceptance criteria (the 9-item checklist — every item must be ticked off)
- Spec §7: edge cases (17 cases — your code handles all of them by following the spec literally)

**When in doubt, follow the spec literally.** The spec was written with rule-level verification. If the spec and your judgment disagree, the spec wins unless the spec is clearly wrong (in which case flag it and ask).

---

Proceed. I will be here when you report back.
