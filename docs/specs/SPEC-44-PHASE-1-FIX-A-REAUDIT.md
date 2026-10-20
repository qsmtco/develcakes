# SPEC-44 Phase 1 — Fix A Re-Audit Brief

**Supervisor:** Supervisor
**Auditor:** Debugger
**Type:** Post-fix verification — your earlier audit said "implementation correct, zero product bugs; Phase 1 must not be accepted as-is because the test suite does not back three acceptance criteria"
**Files changed by Fix A:** `tests/test_tools.py` ONLY. `agent/tools.py` is byte-identical to what you audited.

Load `prompts/adversarialDebugger.md` fresh. Focus on **Section 11 (verify tests match the change)** and re-check the three findings you raised.

**Do not fix anything. Do not edit any file. Report only.**

---

## What Fix A did

| Your BUG | Fix |
|---|---|
| **#1** duplicate tests | `test_search_count_reaches_public_tool_path` rewritten to contrast `execute_tool(...)` against a direct `_search_files(..., count_only=True)` call and assert both outputs are equal — it now proves the *lambda forwards*, which the duplicate did not. |
| **#2** 40-line cap pinned by ZERO tests | `assert r.output.count("  Line ") == 40` added to `test_search_reports_true_total_when_capped`; new `test_search_emission_cap_40_on_large_file` with a 120-match fixture. |
| **#3** four `count_only` behaviours unpinned | 4 new tests: `test_search_count_only_output_is_exact` (single line, no footer/blank/newline), `test_search_count_only_name_only_output_is_exact` (body line must not leak), `test_search_count_only_is_in_the_schema`, `test_search_description_advertises_the_count`. |
| **#4** `file_search` description stale | **DEFERRED by Supervisor** — no model sees that description (`_API_SCHEMA_HIDDEN`), and the schema budget is already +59% over. Recorded for the deferred work item. |
| **#5** schema numbers wrong | **Accepted — my error.** I re-measured the landed tree: coder 6379 / debugger 5944 / supervisor 5924, exactly as you reported. My earlier 6299/5864/5844 matched no real tree state. Corrected in the record. |

---

## Supervisor's own verification (already run — verify, do not just repeat)

All five mutants re-run against the landed tree; every one now produces a failure where before it produced none:

| Mutant | Before Fix A | After Fix A |
|---|---|---|
| `hits[:preview_lines]` → `hits` at `:1142` (breaks the standing order) | 155 passed, **0 caught** | **2 failures**: `assert 41 == 40`, `assert 120 == 40` |
| `count_only` schema property removed at `:1488` | 0 caught | `test_search_count_only_is_in_the_schema` fails |
| description reverted to pre-Phase-1 text | 0 caught | `test_search_description_advertises_the_count` fails |
| `agent/tools.py` unmutated after the round-trip | — | verified: `hits[:preview_lines]` back at `:1142`, `grep` for un-sliced loop returns nothing |

Test counts: `TestSearchFiles` → **21 passed**. Full file → **3 failed, 97 passed** (the 3 pre-existing budget cases).

---

## What to probe

### A. Do the new tests actually bite — independently?
Re-run your own mutants. At minimum:
- **M19b** (drop the emission slice) — must now be caught by TWO tests. Confirm which, and confirm the header assertion alone would still pass (i.e. the new count assertion is what catches it, not a pre-existing one).
- **M4** (`footer always emitted`) — still caught?
- The four #3 mutants you listed: (a) `if count_only:` → `if count_only and hits:`, (b) `lines.append("")` unconditional, (c) schema property removed, (d) description reverted. All four must now fail a test. If any still survives, say which.

### B. Are the new tests sound, or do they over-fit?
- `test_search_count_only_output_is_exact` asserts `r.output.splitlines() == [r.output]`. Is that a genuine single-line assertion, or does it pass vacuously for any non-empty string? (Test it: what does it do if the output ends with `"\n"`?)
- Same question for `test_search_count_only_name_only_output_is_exact`.
- `test_search_emission_cap_40_on_large_file` asserts `"40 of 120 matches"`. Is the `2KB` size prefix in the header stable across environments, or did the Coder correctly avoid asserting it?
- `test_search_description_advertises_the_count` asserts `"count" in description.lower()`. Does that pass vacuously against a description that says "count_only" but never claims a count? Is it strong enough to catch a revert to the pre-Phase-1 text (it must be — verify).
- `test_search_count_reaches_public_tool_path` now asserts `via_tool.output == direct.output`. Would it fail if the lambda edit were reverted? Verify by mutation.

### C. Did Fix A break anything?
- Full `tests/test_tools.py` — 3 pre-existing budget failures only?
- Do the new tests leave any temp files, leak monkeypatches, or depend on execution order? Run `TestSearchFiles` in isolation **and** in a randomized/ reversed order if the harness supports it.
- Is `get_all_tools` used in the schema tests the same one the runtime uses? (If there are two sources of truth, the test could pass against the wrong one.)

### D. Scope discipline
- `git diff` for Fix A must be `tests/test_tools.py` only. The `agent/tools.py` diff is the pre-existing Phase-1 implementation; confirm it contains **no** Fix-A edit by checking that every hunk corresponds to an Edit 1–6 from the Phase-1 instructions.

---

## Deliverable

Report in **BUG #[N]** format: Severity / Assumption violated / Attack vector / Reproduction / Root cause / Fix.

State explicitly **"no bugs found"** if clean.

**If Phase 1 is now clean, say so explicitly and state that Phase 2 may start.** If anything still survives its mutant, name it.

Do not edit files. Do not commit. Report to the Supervisor only.
