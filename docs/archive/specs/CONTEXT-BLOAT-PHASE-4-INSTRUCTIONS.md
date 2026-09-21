# CONTEXT BLOAT — Phase 4 Instructions (Tiktoken-Based Token Estimation)

**For:** QTR (builder)
**From:** Qaster (implementation supervisor)
**Date:** 2026-06-17
**Spec (authoritative contract):** `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-4.md`
**Source bug report:** `docs/bugs/BUG-high-input-token-context-bloat.md` → BUG #5 (MEDIUM)
**Source proposal:** `docs/proposals/PROPOSAL-context-bloat-fix.md` → §5 Phase CB-4
**Target branch:** main
**Depends on:** CB-1, CB-2, CB-3 (all shipped). No code changes from previous phases are required; CB-4 modifies `models/conversation.py` and `pyproject.toml`.

---

## What you're building

**Replace the `chars // 4` token estimation heuristic with `tiktoken`-based accurate counts.** The current heuristic undercounts by ~60% for code-heavy content, which means the trim loop in `Conversation.trim_to_token_limit` stops too early — leaving ~1.5x more tokens than the budget allows. The fix uses `tiktoken.encoding_for_model()` for accurate counts, with multiple fallback layers for unknown models and missing libraries.

**Three pre-flight findings the spec addresses:**

1. **`tiktoken.encoding_for_model` only recognizes bare OpenAI model names.** It raises `KeyError` for `"openai/gpt-4o"` (crabcakes's format). The fix MUST strip the provider prefix via `model_name.split("/", 1)[-1]`.

2. **Existing 4 tests in `TestConversationTokenEstimate` use hard-coded `chars // 4` expectations.** They must be updated to tolerance-based assertions.

3. **`tiktoken` is installed (v0.12.0) but NOT in `pyproject.toml`.** The spec mandates adding it as a runtime dependency.

**Result:** The trim fires at the correct budget. Token monitoring is more accurate. The `chars // 4` heuristic is preserved as the final fallback when `tiktoken` is unavailable.

---

## The spec is the contract

**Read `docs/specs/SPEC-CONTEXT-BLOAT-PHASE-4.md` in full before writing any code.** All 12 sections. The spec was written with rule-level verification against the actual source (every code sample, every signature, every line number was checked). The empirical tiktoken behavior was tested in pre-flight. Do not re-derive the design — implement what the spec says.

**If you find a contradiction between this instructions file and the spec, the spec wins.** Flag the contradiction in the COMPLETENESS checklist and ask me to clarify before implementing.

---

## File scope (everything you will touch)

| File | Change type | Spec section |
|---|---|---|
| `pyproject.toml` | Add `"tiktoken>=0.7"` to `dependencies` | §2.1 |
| `models/conversation.py` | Add `_tiktoken_encoding_for` helper, add `_count_tokens_accurate` method, modify `get_token_estimate` and `get_token_breakdown` | §2.2 |
| `tests/test_conversation.py` | Update 4 existing tests in `TestConversationTokenEstimate`, add new `TestTiktokenAccurate` class (5 tests) | §2.3 |
| `docs/ARCHITECTURE.md` | Add tiktoken note to §3.17 | §2.4 |

**Files you will NOT touch:**

- `models/conversation.py:trim_to_token_limit` — unchanged. It calls `get_token_estimate` which now uses tiktoken. The trim's behavior improves automatically.
- `agent/runtime.py:1288` — unchanged. It calls `get_token_breakdown` which now uses tiktoken. The breakdown's accuracy improves automatically.
- `models/conversation.py:Message` dataclass — unchanged.
- `models/conversation.py:_count_char_tokens` — unchanged. Still used as the fallback when tiktoken is unavailable.
- `tests/test_conversation.py:TestConversationTrim` (4 tests) — unchanged. The trim's behavior with the new estimator is correct.
- `tests/test_phase4.py:TestTrimSummaryInjection` (8 tests) — unchanged.
- `tests/test_agent_runtime.py:TestRunLoopTrimsContext` (1 test) — unchanged.
- `tests/test_agent_runtime.py:TestComputeModelMax` (5 tests) — unchanged.
- `tests/test_agent_runtime.py:TestStreamingUsageCapture` (3 tests) — unchanged.
- `tests/test_agent_runtime.py:TestStuckMessageTransient` (2 tests) — unchanged.
- `tests/test_project_awareness.py:TestAwarenessCaps` (2 tests) — unchanged.

---

## Implementation order (10 steps from spec §5)

Implement in this order. Verify at each step before moving on. **Do not batch.**

1. **Add `tiktoken>=0.7` to `pyproject.toml` dependencies.** Insert after the existing `GitPython>=3.1` line.
   - **Verify:** `grep -n "tiktoken" pyproject.toml` → at least 1 match in the `dependencies` block.
   - **Verify:** `python3 -c "import tiktoken; print(tiktoken.__version__)"` → still works.

2. **Add the `_tiktoken_encoding_for` helper to `models/conversation.py`** (place after `_count_char_tokens` at line 202, before `get_token_estimate` at line 214). The helper strips the provider prefix, calls `tiktoken.encoding_for_model()`, and falls back to `cl100k_base` on `KeyError` or `tiktoken.get_encoding()`.
   - **Verify:** `python3 -c "from models.conversation import _tiktoken_encoding_for; print(_tiktoken_encoding_for('gpt-4o').name)"` → `o200k_base`.
   - **Verify:** `python3 -c "from models.conversation import _tiktoken_encoding_for; print(_tiktoken_encoding_for('openai/gpt-4o').name)"` → `o200k_base` (prefix stripped).
   - **Verify:** `python3 -c "from models.conversation import _tiktoken_encoding_for; print(_tiktoken_encoding_for('claude-3-opus').name)"` → `cl100k_base` (default fallback).

3. **Add the `_count_tokens_accurate` method to `Conversation`** (place after `get_token_estimate` or wherever matches the existing method ordering). The method counts tokens in `system_prompt`, each `msg.content`, each `tc.arguments` (serialized), and each `tc.result`.
   - **Verify:** `python3 -c "import tiktoken; from models.conversation import Conversation; c = Conversation(); enc = tiktoken.get_encoding('cl100k_base'); print(c._count_tokens_accurate(enc))"` → 0 for empty conversation.

4. **Update `get_token_estimate`** to use tiktoken with fallback to `chars // 4`.
   - **Verify:** `python3 -c "from models.conversation import Conversation; c = Conversation(system_prompt='hello world', model='gpt-4o'); print(c.get_token_estimate())"` → 2 (tiktoken o200k_base count).

5. **Update `get_token_breakdown`** to use tiktoken with fallback.
   - **Verify:** `python3 -c "from models.conversation import Conversation; c = Conversation(system_prompt='hello', model='gpt-4o'); c.add_user_message('world'); bd = c.get_token_breakdown(1000); print(bd['system_prompt_tokens'], bd['conversation_tokens'])"` → tiktoken-accurate counts (1 + 1 = 2 tokens).

6. **Update the 4 existing tests in `TestConversationTokenEstimate`** to use tolerance-based assertions. The exact test code is in spec §2.3.1.
   - **Verify:** `pytest tests/test_conversation.py::TestConversationTokenEstimate -v` → all 4 pass.

7. **Add the new `TestTiktokenAccurate` class** with 5 tests (known OpenAI model, provider prefix stripping, unknown model fallback, tiktoken import error, breakdown uses tiktoken).
   - **Verify:** `pytest tests/test_conversation.py::TestTiktokenAccurate -v` → all 5 pass.

8. **Run the full test suite.**
   - **Verify:** `pytest tests/ -q` → all tests pass, no regressions.
   - **Verify:** The existing `TestConversationTrim` (4 tests), `TestTrimSummaryInjection` (8 tests), `TestRunLoopTrimsContext` (1 test), `TestComputeModelMax` (5 tests), `TestStreamingUsageCapture` (3 tests), `TestStuckMessageTransient` (2 tests), and `TestAwarenessCaps` (2 tests) all continue to pass without modification.

9. **Update `docs/ARCHITECTURE.md`** — add the tiktoken note to §3.17.
   - **Verify:** `grep -n "tiktoken" docs/ARCHITECTURE.md` → at least 1 match.

10. **Final self-audit before reporting back:**
    - All 4 production code anchors hit (pyproject dep, helper, accurate method, 2 modified methods)
    - All 9 new tests added (5 in TestTiktokenAccurate + 4 updated existing)
    - Full test suite green
    - Doc updated
    - No collateral edits
    - **No new public API surface** beyond the one new private helper and one new private method

---

## Hard rules (from `prompts/steelFramedCodeWriter.md`)

You MUST follow these. Violating any one is grounds for me to send the work back.

- **Rule 1 (Read Before You Write):** Read every file you will touch in full. Output a discovery block before writing any code.
- **Rule 2 (Hard Part First):** Start with the helper function (step 2) — it's the smallest and most testable unit. Verify it handles all 4 cases (bare name, prefixed name, unknown name, import error) BEFORE moving to the method updates.
- **Rule 3 (Verify Every Claim):** Run the verification commands in steps 1-5 yourself. Don't trust the spec's claimed outputs.
- **Rule 4 (Every Test Must Be Able to Fail):** The `TestTiktokenAccurate` tests must verify the tiktoken counts directly, not just "non-zero." Use `tiktoken.encoding_for_model("gpt-4o").encode(text)` to compute the expected count and assert exact equality.
- **Rule 5 (Wire It Up or Delete It):** The `_tiktoken_encoding_for` helper MUST be called from both `get_token_estimate` and `get_token_breakdown`. If you find yourself defining the helper but only using it in one place, fix the wiring.
- **Rule 5a (Setter-Emitter Pairing):** N/A for this work.
- **Rule 7 (Error Handling):** The helper must catch BOTH `ImportError` (tiktoken not installed) and `KeyError` (model not recognized) and any other `Exception` (download failure, etc.). All three failure modes must be tested. The fallback to `chars // 4` must be the final layer.
- **Rule 8 (Do Not Modify What You Were Not Asked To):** Do not reformat, do not "improve" comments, do not reorder imports. Run `git diff` and verify.
- **Step 6.5 (Test-removal-on-delete):** Not applicable for this work — we're adding new code, not deleting.
- **Step 6.6 (Context-reading requirement):** When the related-bug scan flags "duplicates," read 3+ lines of context. The new `_count_tokens_accurate` method counts tokens in similar fields as `_count_char_tokens` counts chars (system_prompt, msg.content, tc.arguments, tc.result). This is intentional — they are complementary (chars vs tokens), not duplicates. Don't try to "fix" by removing one.
- **Step 6.8 (Spec Drift Verification):** Specs that hardcode line numbers drift as files grow. The spec's `trim_to_token_limit` line citation (269) has already drifted to 251 in the current file. Use `grep -n "def trim_to_token_limit" models/conversation.py` to find the real location. Flag drift >10 lines in the COMPLETENESS checklist.

---

## Adversarial audit (from `prompts/adversarialDebugger.md`)

Before reporting back, run the 11-section adversarial audit against your own changes. Common pitfalls to check for:

- **§1 (Challenge every assumption):** What if `tiktoken.encoding_for_model` is called with a model name that's a substring of a known name (e.g., "gpt-4" vs "gpt-4o")? They have different encodings (cl100k_base vs o200k_base). Verify the spec's `split("/", 1)[-1]` doesn't accidentally match a substring. (It doesn't — it takes the last component after the first `/`.)
- **§2 (Trace the failure backwards):** The original bug is "trim undercounts, leaves too much in history." The fix improves the count. Trace it: better count → trim fires at the right budget → fewer tokens per call. Verify the trim's actual behavior in a real session.
- **§4 (Test weakest links):** What if `tiktoken.encoding_for_model` returns an encoding that doesn't support a specific text (e.g., surrogate pairs)? Unlikely for our use case (we only encode `str`), but the `len(encoding.encode(...))` should not raise. If it does, the helper should catch the exception and return `None`.
- **§5 (Error handling):** What if the encoding download fails (no internet, corrupted cache)? `tiktoken.encoding_for_model` raises some exception. The helper's outer `except Exception` catches it. Verify by reading the helper.
- **§7 (Break the external contract):** What if the user passes `model="openrouter/auto"`? Strip prefix → "auto". `tiktoken.encoding_for_model("auto")` raises `KeyError`. Fallback to `cl100k_base`. Acceptable.
- **§9 (Verify scope coverage):** All 4 files in scope touched? `grep` for the new symbols in each.
- **§11 (Tests match the change):** The 5 new `TestTiktokenAccurate` tests must exercise the new tiktoken-based behavior, not just verify that the helper exists.

**If you find any bug while auditing**, add it to the COMPLETENESS checklist as "Related issue found — not fixed in this phase: [description]" per `steelFramedCodeWriter.md` Step 6.6.

---

## Required output format

After all 10 steps, report back with:

### 1. Files changed (with line numbers)
```
pyproject.toml
  - L<line>: added "tiktoken>=0.7" to dependencies

models/conversation.py
  - L<line>: added _tiktoken_encoding_for helper
  - L<line>: added _count_tokens_accurate method
  - L<line>-L<line>: updated get_token_estimate to use tiktoken with fallback
  - L<line>-L<line>: updated get_token_breakdown to use tiktoken with fallback

tests/test_conversation.py
  - L<line>-L<line>: updated 4 existing tests in TestConversationTokenEstimate
  - L<line>-L<line>: added new TestTiktokenAccurate class (5 tests)

docs/ARCHITECTURE.md
  - L<line>: added tiktoken note to §3.17
```

### 2. Verification outputs (paste the actual command output, not a summary)
```
$ python3 -c "from models.conversation import _tiktoken_encoding_for; print(_tiktoken_encoding_for('gpt-4o').name)"
<paste output, must be o200k_base>

$ python3 -c "from models.conversation import _tiktoken_encoding_for; print(_tiktoken_encoding_for('openai/gpt-4o').name)"
<paste output, must be o200k_base (prefix stripped)>

$ python3 -c "from models.conversation import _tiktoken_encoding_for; print(_tiktoken_encoding_for('claude-3-opus').name)"
<paste output, must be cl100k_base (default fallback)>

$ python3 -c "from models.conversation import Conversation; c = Conversation(system_prompt='hello world', model='gpt-4o'); print(c.get_token_estimate())"
<paste output, must be 2>

$ pytest tests/test_conversation.py::TestConversationTokenEstimate -v
<paste full output, must show all 4 updated tests pass>

$ pytest tests/test_conversation.py::TestTiktokenAccurate -v
<paste full output, must show all 5 new tests pass>

$ pytest tests/ -q
<paste full output, must show 1646+ tests pass with no regressions>

$ grep -n "tiktoken" pyproject.toml
<paste output, must show at least 1 match>

$ grep -n "tiktoken" docs/ARCHITECTURE.md
<paste output, must show at least 1 match>
```

### 3. COMPLETENESS checklist (MANDATORY, exact format)
```
COMPLETENESS:
- [x] Step 1: tiktoken added to pyproject.toml — evidence: <line, grep>
- [x] Step 2: _tiktoken_encoding_for helper — evidence: <line, 3 verification outputs>
- [x] Step 3: _count_tokens_accurate method — evidence: <line>
- [x] Step 4: get_token_estimate uses tiktoken — evidence: <line, verification output>
- [x] Step 5: get_token_breakdown uses tiktoken — evidence: <line, verification output>
- [x] Step 6: TestConversationTokenEstimate updated — evidence: <pytest output>
- [x] Step 7: TestTiktokenAccurate added — evidence: <pytest output>
- [x] Step 8: full test suite green — evidence: <paste pytest output>
- [x] Step 9: ARCHITECTURE.md updated — evidence: <grep>
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
2. Run the 8 verification commands above myself and compare to your pasted output.
3. `git diff` to verify only the specified lines changed.
4. `grep -n` for all the new symbols to confirm placement.
5. Read the actual code in the diff, not your summary.
6. **Independently re-run the tiktoken scenario:** create a `Conversation(model="gpt-4o")`, set system_prompt to a known string, verify the count matches `tiktoken.encoding_for_model("gpt-4o").encode(string)`.
7. **Independently re-run the fallback scenario:** mock `tiktoken` import failure, verify `chars // 4` fallback works.
8. **Independently re-run the prefix-stripping scenario:** verify `"openai/gpt-4o"` and `"gpt-4o"` produce the same counts.
9. Verify no collateral edits in `pyproject.toml` (no other dependency changes).
10. Verify your tests actually exercise the new behaviors (a test that just calls the helper is a helper test, not a behavior test — it would not catch a regression where the helper is wired wrong).

**If I find a bug, I'll send it back with a bug report in the `adversarialDebugger.md` BUG format.** I will not silently fix it myself unless it's a 1-2 line trivial (per implementationSupervisor.md §6).

---

## Word marker

**"please write"** — this is the standing-order word marker per `implementationSupervisor.md` §9.4. Include it in your reply to confirm canonical receipt.

---

## Quick reference: spec sections you'll need

- Spec §2.1: pyproject.toml dependency addition (1 line)
- Spec §2.2.1: `_tiktoken_encoding_for` helper (exact body)
- Spec §2.2.2: `_count_tokens_accurate` method (exact body)
- Spec §2.2.3: `get_token_estimate` update (exact replacement)
- Spec §2.2.4: `get_token_breakdown` update (exact replacement)
- Spec §2.3.1: TestConversationTokenEstimate updates (4 tests, exact code)
- Spec §2.3.2: TestTiktokenAccurate additions (5 tests, exact code)
- Spec §2.4: ARCHITECTURE.md §3.17 note (exact text)
- Spec §5: implementation order (10 steps) — you are here
- Spec §6: acceptance criteria (14 items)
- Spec §7: edge cases (17 cases)

**When in doubt, follow the spec literally.** The spec was written with rule-level verification. If the spec and your judgment disagree, the spec wins unless the spec is clearly wrong (in which case flag it and ask).

---

## Important: this is one focused sub-fix

Unlike CB-2 and CB-3 (which bundled 2-3 independent sub-fixes), CB-4 is a single focused fix: replace `chars // 4` with tiktoken. The implementation is small (~60 production lines + 90 test lines per the proposal). The risk is low. The benefit is the trim fires at the correct budget.

The most subtle part of the implementation is the **provider prefix stripping**. Crabcakes uses `"openai/gpt-4o"` style names, but `tiktoken.encoding_for_model` only recognizes bare OpenAI names. Without the strip, EVERY conversation in this project would raise `KeyError` and fall back to the default encoding (which is `cl100k_base` — close enough but not the actual GPT-4o encoding `o200k_base`).

The second most subtle part is the **existing test update**. The 4 tests in `TestConversationTokenEstimate` use hard-coded `chars // 4` expectations. They MUST be updated to tolerance-based assertions, not just deleted. The new tests verify the same property ("tokens are counted") with a more robust assertion pattern.

---

Proceed. I will be here when you report back.
