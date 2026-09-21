# Phase 4 Instructions — Empty-Assistant-Message Fix (Post-Mortem + Spec Updates)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md`
**Audit reference:** `/path/to/projects/crabcakes/docs/audits/2026-07-05-EMPTY-ASSISTANT-COHERE-400-READ-ONLY.md`
**Phase:** 4 of 4 (final phase — documentation only)
**File scope:** 3 documentation files (no code changes)
**Estimated delta:** ~200 lines across 3 files

> **Reminder:** READ ALL FILES BEFORE STARTING. This phase is documentation-only. No production code is touched.

---

## Context

Phases 1–3 shipped:
- **Phase 1** (read-side filter): commit `4d210bb5fccea9fb47c694b0d70891cd98c2ba3e` — 1 file, 50 lines changed.
- **Phase 2** (write-side guard): commit `0ed7afa9c4465bb1df9b1ea62695439b5bf136a1` — 1 file, 12 insertions, 1 deletion.
- **Phase 3** (regression tests): commit `654bc2038d789d4086ffed49bff0432995386210` — 1 file, 77 insertions, 0 deletions.

Total: 3 commits, 3 files, 65 tests passing (60 existing + 5 new). No regressions.

Phase 4 is the cleanup phase. It does NOT change any production code. It updates documentation to reflect what shipped, fixes spec drift that was discovered during implementation, and writes a post-mortem documenting the incident for future reference.

---

## Tasks

### Task A: Update master spec with delivery status

**File:** `/path/to/projects/crabcakes/docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md`

**Edit 1 — Update header status block.** Find the file's top metadata (lines 1–10) and add a delivery summary at the very top. If the file doesn't have such a block, add it right after the `# ` heading.

Add (or update) the following block under the H1:

```markdown
**Status:** ✅ SHIPPED (2026-07-05)
**Commits:** `4d210bb5fccea9fb47c694b0d70891cd98c2ba3e` (Phase 1), `0ed7afa9c4465bb1df9b1ea62695439b5bf136a1` (Phase 2), `654bc2038d789d4086ffed49bff0432995386210` (Phase 3)
**Tests:** 65 pass (60 pre-existing + 5 new regression tests)
**Branch:** main (1 commit ahead of origin/main after delivery)
**Post-mortem:** `docs/post-mortems/2026-07-05-EMPTY-ASSISTANT-MESSAGE-POST-MORTEM.md`
```

**Edit 2 — Fix spec drift in §2 File 1.** The spec referenced lines 244–258 for the ASSISTANT branch location, but the actual file pre-Phase-1 had the branch at lines 246–260 (+2 line drift). Update the spec to use the **post-Phase-1** line numbers (lines 250–284 per the actual commit).

Find the section heading `### File 1: \`models/conversation.py\`` (or similar) and update any line-number references. The Phase 1 commit moved the ASSISTANT branch from lines 246–260 to lines 250–284 (5 lines added before the branch: `import logging` import costs 1 line, `_logger` declaration costs 2 lines, blank line costs 1 line, comment header costs 1 line; plus the +25 net change inside the branch). **Verify the post-commit line numbers yourself** by reading `models/conversation.py` lines 240–290 after the edits; do NOT trust this estimate blindly.

**Edit 3 — Mark acceptance criteria checkboxes.** Find the sections "## 6. Acceptance Criteria" with subsections "### Functional criteria", "### Test criteria", "### Pattern-sweep criteria", and "### Behavioral criteria (operator-runs)". Each subsection has `[ ]` checkboxes. Mark them `[x]` where the corresponding criterion is satisfied by what shipped. Use this matrix:

- **F1 (substitute placeholder):** ✅ Phase 1 + Test 1
- **F2 (preserve tool_calls case):** ✅ Phase 1 + Test 3
- **F3 (preserve text-only case):** ✅ Phase 1 + Test 4
- **F4 (warning log):** ✅ Phase 1 + Tests 2, 5
- **F5 (no `("", [])` in agent/):** ✅ Phase 2
- **F6 (placeholder passed at runtime.py:2214):** ✅ Phase 2
- **T1 (5 new tests pass):** ✅ Phase 3
- **T2 (7 existing tests unchanged):** ✅ Phase 3 (line 191 unchanged)
- **T3 (12 total):** ✅ Phase 3 (7 + 5 = 12)
- **T4 (full suite passes — minus approval):** ✅ Phase 3 (65 pass — note: spec said 700+ which was wrong; actual is 65 in test_conversation.py alone; the full suite is much bigger; run it and report the actual count)
- **T5 (95 runtime tests pass):** ⚠️ Cannot verify without running test_agent_runtime.py. Run it; if it passes, mark [x]; if it fails, mark [ ] and flag.
- **P1 (no `("", [])` anywhere):** ✅ V4 of Phase 2
- **P2 (no `""` add in tests):** ✅ Need verification (grep yourself)
- **P3 (to_api_messages call sites unchanged):** ✅ Phase 2 audit
- **P4 (single `import logging`):** ✅ Phase 1
- **B1 (stopgap finds zero corrupt messages):** ⚠️ Operator action — leave [ ] but note in post-mortem
- **B2 (Cohere no longer 400s):** ⚠️ Operator action — leave [ ] but note in post-mortem

For P2 verification, run `grep -rn 'add_assistant_message\(""' agent/ models/ tests/` and `cd /path/to/projects/crabcakes && grep -rn '"role":\s*"assistant".*"content":\s*""' agent/ models/ tests/` (both should return zero matches against current code).

For T4/T5 verification, run:
- `cd /path/to/projects/crabcakes && pytest tests/test_conversation.py 2>&1 | tail -3` (confirm 65 pass)
- `cd /path/to/projects/crabcakes && pytest tests/test_agent_runtime.py -k "not approval" 2>&1 | tail -3`

**Edit 4 — Extend §10 (Backlog) with post-shipment findings.** Spec §10 already has 5 deferred items. Add 3 more (B-1, B-2, B-3) capturing decisions made during Phases 1–3 about items that came up but were consciously deferred. Insert at the bottom of the §10 section (before any blank-line "End of spec" marker):

```markdown
6. **`agent/runtime.py:2290` write-side guard tightening** — when `text_content` is `""` but `response.get("choices")` is non-empty (truthy), the write-side guard at line 2213 does NOT fire (it requires BOTH to be falsy). The code falls through to `conv.add_assistant_message(text_content, [])`, persisting an empty-content message. Phase 1's read-side filter catches it at serialization time (defense-in-depth) but the write path remains inconsistent. **Decision (2026-07-05):** Out of scope. The read-side filter is sufficient. If the guard is to be tightened, the fix is to check `if not text_content` (without the choices condition) — but this would also re-classify any "choices returned empty content" case as an error, which is a semantic change. Flagging for future work.
7. **`add_assistant_message` `ValueError` validation (`spec §10 item 5`)** — the spec originally listed this as deferred. **Decision (2026-07-05):** Reject this proposal. Runtime call sites now use placeholders; test call sites use empty strings intentionally (to test the read-side filter). A validation guard would break the tests' intentional empty-content assertions. If added, would need to gate on `tool_calls` being non-empty too. Removing from backlog to avoid future-me re-considering it.
8. **Two distinct placeholder strings** — `Phase 1: "[assistant returned no content — placeholder]"`; `Phase 2: "[LLM returned no choices and no content — provider error or malformed response]"`. Deliberately distinct so log analysis can tell creation events (Phase 2) from transit events (Phase 1) apart. **Decision (2026-07-05):** Keep distinct for now. Re-evaluate if user feedback says it's confusing. Track in `docs/post-mortems/2026-07-05-EMPTY-ASSISTANT-MESSAGE-POST-MORTEM.md` §9 backlog.
```

**Don't renumber the existing 5 items.** They stay 1–5. New items become 6, 7, 8.

**Edit 5 — Spec drift in §2 File 2.** The spec said the write-side guard was at line 2214; Qaster confirmed the actual code line was 2214 (no drift here per Qaster's Step 6.8 check). No fix needed.

**Edit 6 — Spec drift in §7 edge case table.** The spec mentioned a test `test_empty_assistant_in_full_sequence_substituted_in_place`. We did not write that exact test — instead we wrote `test_corrupt_message_mid_sequence_uses_correct_index_in_warning` which covers the same ground. Update the spec table to reflect actual test names:

Find the row "Empty assistant in the middle of a long sequence" and update the Test column from `test_empty_assistant_in_full_sequence_substituted_in_place` to `test_corrupt_message_mid_sequence_uses_correct_index_in_warning (Phase 3)`.

### Task B: Write the post-mortem

**New file:** `/path/to/projects/crabcakes/docs/post-mortems/2026-07-05-EMPTY-ASSISTANT-MESSAGE-POST-MORTEM.md`

Use the post-mortem style from `docs/post-mortems/2026-07-04-CODER-400-STALE-MESSAGES-POST-MORTEM.md` as a template. Target length: ~150–250 lines.

**Required sections:**

```markdown
# Empty Assistant Message HTTP 400 (Cohere) Post-Mortem

**Date:** 2026-07-05
**Severity:** High (provider-specific failure blocking supervisor functionality)
**Trigger:** Supervisor agent → Cohere (north-mini-code:free) → HTTP 400 "messages.0: must have non-empty content or tool calls"
**Detection:** Audit by user; logged in `docs/audits/2026-07-05-EMPTY-ASSISTANT-COHERE-400-READ-ONLY.md`
**Status:** ✅ FIXED (Phases 1–3 shipped 2026-07-05)
**Commits:** `4d210bb5`, `0ed7afa9`, `654bc20`

---

## 1. Code Quality Grade: [self-assess per template]

[Fork from existing post-mortem style: lead with a grade and justified table]

---

## 2. Reproduction (provider matrix)

| Provider | Result | Why |
|----------|--------|-----|
| Cohere (north-mini-code:free) | ❌ HTTP 400 | Strict schema validation rejects `{"role":"assistant","content":""}` |
| OpenAI (gpt-4, mini) | ✅ Accepts (silently) | Tool calls path tolerates empty content; some models accept |
| Anthropic (Claude 3.x) | ⚠️ Strict mode rejects | Anthropic strict-tool-loop enforces "must have non-empty content or tool calls" |
| M-series (m2.7) | ✅ Accepts (silently) | Permissive, no schema validation |

**Conclusion:** A defect that affects 2 of 4 production providers (Cohere, Anthropic strict). The fix must be provider-agnostic.

---

## 3. Root Cause

### 3.1 Trigger path

[From audit §2.3: "How it likely got there"]

### 3.2 The bug

`agent/runtime.py:2214` (pre-fix) called `conv.add_assistant_message("", [])` whenever an LLM returned no `text_content` AND no `choices`. The empty string was persisted to disk. On subsequent calls, `to_api_messages()` had no filter and emitted the corrupt `{"role":"assistant","content":""}` to the wire. Strict providers rejected this with HTTP 400.

### 3.3 Why it slipped through

- `runtime.py:2214` was added as a defensive path for "LLM returned nothing" — the intent was to record SOMETHING so the conversation history preserved the error state.
- The empty-string + empty-list combination was chosen as "least surprise" — it represents "the assistant said nothing," which is literal truth but semantically corrupt for the API.
- `to_api_messages()` had no filtering logic — every message was serialized as-is.
- No test covered `Message(role=ASSISTANT, content="")` case — happy paths only.
- The bug was provider-specific: M-series and permissive OpenAI didn't catch it, so it slipped through developer testing. Production users using Cohere or Anthropic strict tripped immediately.

---

## 4. Fix (Phases 1–3)

### 4.1 Phase 1 — read-side filter

`models/conversation.py:to_api_messages()` ASSISTANT branch. When `not msg.content and not msg.tool_calls`, substitute the placeholder string `"[assistant returned no content — placeholder]"` and emit a warning log. Defense-in-depth: catches any corrupt message regardless of source.

Commit: `4d210bb5fccea9fb47c694b0d70891cd98c2ba3e` ("Accept: models/conversation.py")
Δ: +37/-13 lines, file 452 → 476 lines (added `import logging`, `_logger`, new branch logic)

### 4.2 Phase 2 — write-side guard

`agent/runtime.py:2222` (was line 2214 pre-fix). Replace `conv.add_assistant_message("", [])` with `conv.add_assistant_message("[LLM returned no choices and no content — provider error or malformed response]", [])`. Persists a descriptive placeholder instead of empty string. Keeps `logger.warning(...)` (error logging), keeps `self._dispatch(self._on_error, ...)` (user UX), keeps `self._auto_save(...)` and `return`. Surrounding 5 lines of code unchanged.

Commit: `0ed7afa9c4465bb1df9b1ea62695439b5bf136a1` ("Accept: agent/runtime.py")
Δ: +11/-1 lines

### 4.3 Phase 3 — regression tests

`tests/test_conversation.py::TestConversationToApiMessages` gained 5 new tests:

1. `test_empty_assistant_message_substitutes_placeholder` — locks in placeholder substitution
2. `test_empty_assistant_message_logs_warning` — locks in WARNING log with idx info
3. `test_assistant_message_with_only_tool_calls_does_not_substitute` — regression guard (placeholder must NOT fire when tool_calls is non-empty)
4. `test_assistant_message_with_only_content_does_not_substitute` — regression guard (placeholder must NOT fire on normal text)
5. `test_corrupt_message_mid_sequence_uses_correct_index_in_warning` — locks in idx=2 for corrupt at mid-sequence position

Commit: `654bc2038d789d4086ffed49bff0432995386210` ("Accept: tests/test_conversation.py")
Δ: +77 lines, 0 deletions (zero modifications to existing tests)

---

## 5. Test Results

| Test | Before | After |
|------|--------|-------|
| Total `tests/test_conversation.py` | 60 pass | 65 pass |
| `TestConversationToApiMessages` tests | 7 | 12 (7 unchanged + 5 new) |
| `tests/test_agent_runtime.py -k "not approval"` | unknown | [run, paste result] |
| Full project test suite | unknown | [run, paste result] |

---

## 6. What's Good About the Implementation

[4–6 bullets in the style of existing post-mortems]

1. **Spec-driven, phased delivery:** Master spec at `SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md` defined 4 phases with testable acceptance criteria. Each phase was independently auditable (V1–V7 verifications), preventing scope creep and ensuring each commit could be reverted if needed.
2. **Defense in depth:** Two layers (read-side filter + write-side guard) catch the same bug from different angles. The read-side filter is the safety net for corrupt messages from any source (compaction, hand-edits, future bugs). The write-side guard is the primary prevention. Both work together.
3. **Distinct placeholders for creation vs transit:** Phase 2's placeholder ("LLM returned no choices...") says "we know why this happened." Phase 1's placeholder ("assistant returned no content...") says "we found this corrupt, don't know why." Distinguishing these in logs makes future debugging easier.
4. **Regression tests, not new tests:** Phase 3 added tests that lock in behavior (replace placeholder string in code → test 1 fails). The two regression-guard tests (3, 4) ensure the read-side filter doesn't over-fire.
5. **No infrastructure for write-side tests:** Phase 2 was intentionally not tested in `tests/test_conversation.py` because it requires faking `AgentRuntime` + mocking the LLM provider. That's heavy infrastructure for a 1-line edit. The user's audit verifies the runtime path by switching provider to Cohere (B2 acceptance criterion).
6. **Spec drift caught and documented:** The spec referenced ASSISTANT branch at lines 244–258; actual was 246–260 (+2 line drift). Caught during Phase 1 implementation, documented in this post-mortem. The spec is corrected in Phase 4.

---

## 7. What's Bad About the Implementation

[2–4 honest bullets]

1. **Spec drift of +2 lines:** Should have been caught at spec-writing time. The audit doc was 2 days old (2026-07-05) and line numbers had drifted between original spec and actual file. Future specs should run `wc -l` on each referenced file at write time and double-check anchors immediately before dispatching.
2. **Related bug at line 2290 left unfixed:** `agent/runtime.py:2290` still calls `conv.add_assistant_message(text_content, [])` where `text_content` could be empty (if `choices` is non-empty). The Phase 2 guard only catches `not text_content AND not choices`. Caught by Qaster during Step 6.6 related-bug scan. Deferred as B-1 backlog. Defense-in-depth (Phase 1 read-side filter) catches it, so no production impact.
3. **Two placeholder strings may confuse downstream parsing:** If a future feature searches conversation history for "exact-empty-assistant messages" by string match, the two placeholders create ambiguity. Distinct placeholders are correct for log discrimination but break simple string-equality checks. Document the two strings' meanings somewhere visible to anyone iterating on conversation parsing logic.

---

## 8. Behavioral Verification (operator-runs)

### B1 — Stopgap script result

[Operator action — leave blank or note "Operator has not yet run the stopgap against `~/.config/crabcakes/conversations/special:supervisor.json`"]

### B2 — Cohere retry test

[Operator action — leave blank or note "Operator has not yet verified Cohere does not 400 on subsequent calls"]

---

## 9. Backlog

- **B-1 (now §10 item 6):** `agent/runtime.py:2290` write-side guard tightening (see `SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md` §10 item 6)
- **B-2 (now §10 item 7):** `add_assistant_message` `ValueError` validation (rejected; would break intentional empty-content test cases) — see spec §10 item 7
- **B-3 (now §10 item 8):** Consolidate the two placeholder strings if user reports confusion (rejected; distinct strings are deliberate) — see spec §10 item 8

---

## 10. Process Improvements

1. **Spec line-anchoring:** Add a step to `steelFramedSpecWriter.md` that runs `wc -l` and `grep -n` on each referenced file before dispatching, so line-number drift is caught at spec time, not implementation time.
2. **Related-bug-scan as standard:** Qaster's Step 6.6 found the line 2290 bug. Add this step to the standard Coder/Debugger prompts explicitly so related bugs are surfaced in every report, not just the ones the builder happens to notice.
3. **Distinguish transit vs creation placeholders:** The two-placeholder-string pattern from this work should be a documented pattern. If two event types (creation vs discovery) need to be distinguishable in logs, use distinct strings.

---

## End of post-mortem.
```

**Style notes:**
- Tone is matter-of-fact, not blame-shifting.
- Numbers are exact (commit hashes, line numbers, test counts) — pulled from git/diffs.
- "What's Good / What's Bad" is honest, not promotional.
- Backlog is actionable, with explicit decisions (accepted/rejected).
- Process improvements propose concrete edits to prompt files.

### Task C: Update audit doc with resolution status

**File:** `/path/to/projects/crabcakes/docs/audits/2026-07-05-EMPTY-ASSISTANT-COHERE-400-READ-ONLY.md`

**Edit 1 — Add status header.** Insert at the very top (right after the H1):

```markdown
**Status (2026-07-05):** ✅ RESOLVED. Phases 1–3 shipped. See post-mortem at `docs/post-mortems/2026-07-05-EMPTY-ASSISTANT-MESSAGE-POST-MORTEM.md` and updated spec at `docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md` (Status section).
```

**Edit 2 — Check the recommended-actions boxes.** Find §4 ("Recommended actions (NOT applied — read-only per request)") and update the subsection headers to mark them as APPLIED:

```
### 4.1 Stopgap: repair the corrupt supervisor conversation file (APPLIED — pending operator verification)
### 4.2 Proper fix: add validation in `to_api_messages` (APPLIED — Phase 1, commit 4d210bb5)
### 4.3 Companion fix: add content validation in the add path (REJECTED — see spec §10 item 7; write-side guard at runtime.py:2222 is the primary prevention)
```

For §4.2, change the `Recommended actions (NOT applied)` line in §4's preamble to `Recommended actions (MIXED — applied in Phases 1–3; details below)`.

That's all for Task C.

---

## What NOT to Change

- **Do NOT touch** `agent/runtime.py`, `models/conversation.py`, `tests/test_conversation.py`. Phase 4 is documentation only.
- **Do NOT touch** `tests/`, `agent/`, or `models/` directories.
- **Do NOT modify** `docs/post-mortems/2026-07-04-CODER-400-STALE-MESSAGES-POST-MORTEM.md` or any existing post-mortem.
- **Do NOT touch** the phase instruction files (`EMPTY-ASSISTANT-MESSAGE-FIX-PHASE-1-INSTRUCTIONS.md`, etc.) — they're historical record.
- **Do NOT add new audit files, new test files, or new spec files.** Phase 4 updates 2 existing files and creates 1 new post-mortem.
- **Do NOT rebrand** the placeholder strings. Keep them exactly as they are in the code.

---

## Verification Commands (run all, paste full output)

### V1. Spec status block added

```
cd /path/to/projects/crabcakes && head -15 docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md
```

Expected: status block visible at top with `✅ SHIPPED`, three commit hashes, `65 pass`, post-mortem link.

### V2. Spec drift fixed

```
cd /path/to/projects/crabcakes && grep -n "^### File 1\|^### File 2" docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md
```

Then read those sections and verify the line-number references match the post-Phase-1 code. Specifically: ASSISTANT branch references in §2 should point to ~250–284 (post-Phase-1) not 244–258 (pre-Phase-1).

### V3. Acceptance criteria marked

```
cd /path/to/projects/crabcakes && grep -c '\[x\]' docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md
cd /path/to/projects/crabcakes && grep -c '\[ \]' docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md
```

Expected: at least 12 `[x]` (the box-checkable criteria that are shippable). Exactly 2–3 `[ ]` (B1, B2, possibly T5 if test_agent_runtime.py has failures).

### V4. Backlog items 6/7/8 added to §10

```
cd /path/to/projects/crabcakes && grep -n "^6\. \`agent/runtime.py:2290\|^7\. \`add_assistant_message\`\|`ValueError\|^8\. \*\*Two distinct placeholder strings" docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md
```

Expected: 3 hits, all in §10 (between line 577 and end of file). Items numbered 6, 7, 8 (existing items 1–5 unchanged).

### V5. Post-mortem exists and has all required sections

```
cd /path/to/projects/crabcakes && ls -la docs/post-mortems/2026-07-05-EMPTY-ASSISTANT-MESSAGE-POST-MORTEM.md
cd /path/to/projects/crabcakes && grep -n "^## 1\.\|^## 2\.\|^## 3\.\|^## 4\.\|^## 5\.\|^## 6\.\|^## 7\.\|^## 8\.\|^## 9\.\|^## 10\." docs/post-mortems/2026-07-05-EMPTY-ASSISTANT-MESSAGE-POST-MORTEM.md
```

Expected: file exists, 10 numbered sections all present.

### V6. Audit doc status updated

```
cd /path/to/projects/crabcakes && head -15 docs/audits/2026-07-05-EMPTY-ASSISTANT-COHERE-400-READ-ONLY.md
```

Expected: status block visible with `✅ RESOLVED`.

### V7. Final test suite still passes

```
cd /path/to/projects/crabcakes && pytest tests/test_conversation.py 2>&1 | tail -3
```

Expected: 65 passed. Phase 4 is doc-only; tests should be unchanged.

### V8. No production code touched

```
cd /path/to/projects/crabcakes && git status --short
cd /path/to/projects/crabcakes && git diff HEAD --stat
```

Expected: only docs modified. No changes to `agent/`, `models/`, or `tests/` (other than the already-accepted Phase 3 test_conversation.py edit).

---

## Related-Bug Scan (Step 6.6)

Before reporting done, scan for:

1. **Spec drift in OTHER places.** The §2 file references for Phase 2 — are the line numbers still right after Phase 2 shipped? (Qaster confirmed in Step 6.8 — line 2214 matches pre-edit code. No drift there.)
2. **Section "Cross-phase invariants" in the spec.** Does each invariant now hold? Run the verifications listed in the spec §5:
   - After Phase 1: `pytest tests/test_conversation.py` — pass with original 7 tests, no new tests ✅
   - After Phase 2: `pytest tests/test_agent_runtime.py -k "not approval"` — should pass; verify and report
   - After Phase 3: 12 total tests in TestConversationToApiMessages ∪ TestToApiMessagesEmptyAssistantGuard — verify (we don't have TestToApiMessagesEmptyAssistantGuard as a separate class; we added 5 tests to TestConversationToApiMessages. Spec §5 has a class-name drift. Note this and decide: either rename the new tests into a separate class, or update the spec to reflect "12 tests in TestConversationToApiMessages" instead of "7 + 5 split across two classes". **DECISION:** Update the spec, don't create a new class. Splitting into a new class for 5 tests is artificial.)
3. **Section 9 of audit doc** (Self-audit answers). Are any of those questions now answered differently because of the fix? Spot-check.

Document any findings in the related-bug scan section of your report.

---

## Reports Required

When complete, reply with:

1. **Discovery block** (DISCOVERY: list of files read — the existing post-mortem template, the audit doc, the master spec, the 3 commits, and what you learned)
2. **Files changed** with `git diff --stat` output (should show only docs/)
3. **Full output** of verification commands V1–V8
4. **COMPLETENESS checklist**
5. **Related-bug scan** findings (Step 6.6)
6. **Implementation-choice rationale** (Step 6.7) — for Phase 4 the choices are:
   - (a) Why no separate `TestToApiMessagesEmptyAssistantGuard` class? (Answer: 5 tests in TestConversationToApiMessages keeps related tests together. Creating a new class for 5 tests is artificial and forces splits in maintenance. Spec §5 wording was just slight drift.)
   - (b) Why keep two distinct placeholder strings in the post-mortem rather than consolidating? (Answer: distinct strings are deliberate for log discrimination. The post-mortem documents this so future maintainers don't "helpfully" merge them.)
   - (c) Why use the audit doc for status update rather than a new "resolution" doc? (Answer: the audit doc is the original bug report; updating its status in-place creates a single source of truth for "what was the bug and how was it resolved." A separate resolution doc would force readers to cross-reference.)
   - (d) Why not add a regression test for the audit doc's stopgap (§4.1)? (Answer: the stopgap is operator-run against on-disk conversation files, not production code. There's no clean place to test it in pytest. Operator verification is the right gate.)
   - If you made other choices, document them.
7. **Spec drift check** (Step 6.8) — confirm spec's Phase 1 file references match post-Phase-1 line numbers; confirm spec's §5 cross-phase invariants match actual delivery.

**Do not declare done** unless ALL of V1–V8 pass and the COMPLETENESS block is present.

---

## End of Phase 4 instructions.

Phase 4 is the final phase. After Phase 4 ships, the EMPTY-ASSISTANT-MESSAGE-FIX initiative is closed. Any future regressions or related work should be tracked under new initiatives, not appended to this spec.