# SPEC-44 Re-Audit Brief (rev. 3)

**Date:** 2026-10-20
**Supervisor:** Supervisor
**Auditor:** Debugger
**Type:** Second re-audit — rev. 2 was "NOT YET IMPLEMENTABLE — 4 blocking findings"
**Spec under audit:** `docs/specs/SPEC-44-SEARCH-FILES-COUNTS-CONTEXT.md` — now **rev. 3**, 608 lines (rev. 1: 396, rev. 2: 517)

Load `prompts/adversarialDebugger.md` fresh. Work through all 11 sections again against the revised spec and the same code scope. One probe per section minimum.

**Do not fix anything. Do not edit any file. Report only.**

---

## What changed in rev. 3

The four **blocking** findings from your re-audit, plus the eleven non-blocking accuracy findings. Every correction is marked inline with its BUG number.

| BUG | Fix in rev. 3 |
|---|---|
| **#21** (BLOCKING) | §2.1 now uses `rel in name_matches` — the **existing local** in `_search_files`, which the Supervisor verified is assigned in both the file-path and directory branches and is in scope at the emission loop. The spec now says explicitly: *do NOT invent a `name_match_set` identifier*; if a set is wanted it must be built explicitly before the loop, and the spec does not require it. §3 step 7 and §5 step 1 both carry it. |
| **#22** (BLOCKING) | §2.3 defines `K` over **content-hit headers only**: `listed_content = sum(1 for r in ordered if grep_hits.get(r))`, `K = len(grep_hits) - listed_content`, marker fires when `K > 0`. The spec includes the reproduction table showing the naive `M - len(ordered)` formula failing to fire at all in the 10-name/20-content row. §3 step 7 now requires the implementation to track `listed_content`. |
| **#23** | §2.3: `N` and `M` now include name-only match files. §7 gained a row: a name-only-only search reads `[N matches in M files]`, never `[0 matches in 0 files]`. |
| **#24** (BLOCKING) | §2.2 mandates dropping the trailing empty element after `split("\n")`: `if lines and lines[-1] == ""]: lines.pop()`. §7 gained a row; `test_search_context_at_file_edges` must now assert the **absence** of a trailing empty context line. |
| **#25** | §8 publishes the **measured** sizes: coder 6503 / debugger 6068 / supervisor 6048, a +254 delta per agent (properties +96, description +72). |
| **#26** | §2.1 states 146 chars vs the current 112 (+34); the Phase-2 description is 184 (+72). |
| **#27** (BLOCKING) | §2.2 now states the match line is emitted **exactly once, inside the merged block**, using grep's **stripped** content, while context lines use the file's **raw** line. A table pins which source renders which. The example block is regenerated. §3 step 7 rewritten to match. |
| **#28** | §2.2 corrects the false claim: the 16 KB bound is a **REFUSAL, not a window** (`_read_file` serves the first 16 KB with a notice). The consequence on this repo is stated explicitly (7 of 27 `agent/*.py` files over 16 KB, including `agent/tools.py` and `agent/runtime.py`). §7 gained a row. |
| **#29** | DISCOVERY no longer claims "12 of 30"; it states the number varies with line length (2–12 of 30 in the probes run) and that the meaningful fact is the middle being dropped. |
| **#30** | §2.3 now reconciles BOTH truncation layers: the 100 KB cut keeps the HEAD, the 16 KB bound keeps a 2 KB head + 8 KB tail. **The summary is therefore emitted FIRST**, before the per-file groups, so it survives both. §7's two truncation rows are aligned. |
| **#31** | New **§2.6 Phase-3 test table** with four named tests: `test_search_summary_reports_true_totals`, `test_search_summary_marks_unlisted_files`, `test_search_summary_emitted_in_count_only`, `test_search_summary_counts_name_only_files`. §5 step 10 lists them as its exit condition. |
| **#32** | §2.3: in `count_only` mode **both** the trailing empty line and the footer are omitted. |
| **#33** | §6 and §2.5 now say "**starts with** `  Line `", and the test table notes that a context line containing the file's own `  Line {lineno}:` source text is legitimate. |
| **#34** | §5 rewritten: 13 steps, each with a named exit condition (a specific test or a specific grep). The duplicate step 4 was folded into step 3's evidence. |
| **#35** | §2.1 notes the `0 matches` branch is defensive and unreachable, and the acceptance criterion now asserts the header **TEXT** (`name match`) rather than the absence of `0 matches` — the criterion that could not fail. |

---

## Code scope to probe

Unchanged: `agent/tools.py` — `_search_files` (~962), `_run_grep` (~890), the `split(":", 2)` hit parser, the emission loop (~1090-1119), `_file_search` (~1117), the `search_files` and `file_search` registrations (~1430-1479), constants (~270-285), `execute_tool` (~1808), `bound_tool_output` (~1742-1764); `agent/context.py` `_find_matching_files` (956); `tests/test_tools.py` `TestSearchFiles` (~589-660), `TestToolSchemaSizeBudget` (~1152).

---

## What to probe now

### A. Are the four blocking findings actually fixed — correctly, not cosmetically?
For each, verify against the SOURCE, not the spec prose:
- **#21**: is `name_matches` genuinely in scope at the emission loop, in all three paths (directory search, subdirectory-root search, explicit file path)? Is `rel in name_matches` correct when a file is BOTH a name match and a content match? Is there any path where a file appears in `ordered` but in NEITHER `name_matches` nor `grep_hits` — which would make `name_only` False and `total` 0, hitting the defensive branch? Check that path explicitly.
- **#22**: is `listed_content` computable at the point the summary is emitted? The summary is now emitted FIRST, before the emission loop — but `listed_content` depends on `ordered` and `grep_hits`, which are both known before the loop. Verify the ordering is sound. Also verify the `K` formula against your own fixture set, including the 10-name/20-content row.
- **#24**: is dropping the trailing empty element correct for a file that is genuinely EMPTY (zero bytes)? `"" .split("\n")` → `[""]` → after pop → `[]`. Does any downstream code index into that list? Does an empty file ever reach the context reader (it has no hits, so it should not — verify).
- **#27**: is "the match line uses grep's stripped content, the context line uses the file's raw line" internally consistent with how the merge window is built? If the match line is rendered from grep's content but the window is built from the file's lines, there are two sources for the same line — verify the implementation can render each exactly once and identically to the pinned format.

### B. New bugs introduced by rev. 3
- The summary is now emitted FIRST — does that break the existing `"(no matches)"` exact-match test, or any test that assumes the output starts with a file path? Check `test_search_no_match`, `test_search_finds_match`, `test_search_file_type_filter`, `test_search_file_path_returns_the_line` against a leading summary line.
- The summary in `count_only` mode: the spec says the trailing empty line and the footer are both omitted, and the summary is the final line — but the summary is emitted FIRST. Those two statements conflict. Which is it? This may be a NEW inconsistency.
- Does emitting the summary first break the `MAX_EXEC_OUTPUT` truncation semantics in a way the spec describes? The 100 KB cut keeps the head, so the summary survives — but the spec should say the summary is not counted in the truncation.
- §5 now has 13 steps with exit conditions. Are any of them circular, or dependent on a test that does not exist yet at that step?
- The Phase-3 tests (§2.6) are 4 new tests — do any duplicate the Phase-1/2 tables?

### C. Spec-internal consistency (whole document)
- §2.3 vs §3 step 8 vs §6 vs §7 on the summary: format, population, `K` formula, and **position** (first vs last) — do all four agree now?
- §2.1 vs §2.2 vs §8 on the description: 146 chars (Phase 1) and 184 (Phase 2) — verify by counting the actual strings in the spec.
- §2.5's `test_search_context_zero_is_unchanged` — with the summary now emitted first, does "byte-identical" still hold between the `context=0` and no-parameter calls? (Both would have the summary, so yes — but verify the spec says so.)
- §6: are all acceptance criteria now covered by a named test? You found 11 uncovered in rev. 2; check the count now.

### D. Scope discipline
Re-verify the spec still does NOT: change `max_results` reachability, fix the schema budget, add a grep refusal, change `_find_matching_files`, or touch `agent/runtime.py`, `agent/context_strategy.py`, `agent/repo_map.py`.

---

## Deliverable

Report in **BUG #[N]** format: Severity / Assumption violated / Attack vector / Reproduction / Root cause / Fix.

State explicitly **"no bugs found"** if the probe comes back clean.

**If the spec is now implementable, say so explicitly.** Name anything still blocking. If it is not, name exactly which BUGs remain and what the spec must say. If the remaining issues are non-blocking accuracy items, say that plainly and name the implementation as safe to start.

Do not edit files. Do not commit. Report to the Supervisor only.
