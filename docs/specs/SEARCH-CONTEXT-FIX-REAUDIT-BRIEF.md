# RE-AUDIT BRIEF — search_files context repair, Phase 2 (uncommitted on c0ffc179)

**Auditor:** Debugger. Load `.crabcakes/prompts/adversarialDebugger.md` fresh, work all
11 sections against the code in scope. BUG #[N] format
(Severity / Assumption violated / Attack vector / Reproduction / Root cause / Fix).
State explicitly "no bugs found" per clean section.

**This is a re-audit of the Phase-2 fix for your own Phase-1 report.** Your five
findings were accepted. Four were independently reproduced by the Supervisor
(BUG #1, #2, #3, #4); BUG #5 was accepted as read.

**Code scope (uncommitted on top of `c0ffc179`):**
- `agent/tools.py` — `SEARCH_TOTAL_CHARS` (~:289), `_emit_search_hits` (~:1314-1370),
  `_search_files` aggregate loop (~:1522-1558), docstring (~:1389), description (~:1900)
- `tests/test_tools.py` — 8 new tests total (4 from Phase 1, 4 from Phase 2)

**Contracts:** `SEARCH-CONTEXT-FIX-PHASE-1-INSTRUCTIONS.md` (done),
`SEARCH-CONTEXT-FIX-PHASE-2-INSTRUCTIONS.md` (this round).

---

## What Phase 2 claims to have fixed

- **Your BUG #1 (HIGH):** `SEARCH_TOTAL_CHARS = 12_000` aggregate budget. Per-file
  budget becomes `min(SEARCH_FILE_CHARS, SEARCH_TOTAL_CHARS - spent)`; when the
  remainder is <= 0 the radius is forced to 0 but match lines still emit.
- **Your BUG #2 (MEDIUM):** file read once per merged window; the shrink loop only
  trims the already-read `sliced` dict.
- **Your BUG #3 (MEDIUM):** per-window allowance `budget // n_windows`; each window
  shrinks only itself.
- **Your BUG #4 (MEDIUM):** two new tests to kill M4 (truncated match lines) and
  M11 (dropped match lines).
- **Your BUG #5 (LOW):** description and `_search_files` docstring reworded per-file.

## Supervisor verification already done — probe past it, look for what it misses

| Check | Result |
|---|---|
| BUG #1 fixture: 20 files x 1 hit | **12796 chars, 20/20 match lines, no omission** (Phase 1 was 10271 / 3-of-20; `c0ffc179` was 938 / 20-of-20) |
| BUG #2: `_context_slice` calls for one 1-hit file | **1** (Phase 1 was 48) |
| BUG #3 fixture: narrow + wide hit, radius 15 | narrow window lines 1-9 **all present**, 26 ctx lines |
| Original defect A fixture | 3296 chars, match visible, 6 ctx lines |
| Per-file radius 1/3/4 files x 1 hit | 99 / 297 / 396 ctx lines |
| Suites | 189 passed, 3 failed — only pre-existing `TestToolSchemaSizeBudget` (6926/6491/6471 vs 4000) |
| ruff | **identical rule multiset to `c0ffc179`** (20 findings) |
| Untracked artifacts | Coder left `tests/_pytest_junit.xml` + `_pytest_junit2.xml`; Supervisor removed them |

---

## Seven things to attack

1. **Does `spent` actually bound the output?** Coder's arithmetic assumes blocks
   <= 12,000 plus ~480 chars of headers plus summary/footer. The Supervisor measures
   12,796 on the 20-file fixture — under the 16,384 cap, but only ~3.6 KB of slack.
   Construct fixtures that inflate the **uncounted** parts: long relative paths in
   headers (`rel` is in every header AND every context line prefix), a large
   `unlisted` summary, name-only files, `count_only` off with 20 hard-linked files,
   deep nesting. Can the total exceed `CONV_OUTPUT_CAP` and drop match lines again?
   Coder self-flagged this as related issue #2 — quantify it.

2. **`budget // n_windows` starvation when windows are many.** With 3 hits the
   allowance is `4000 // 3 = 1333`. With many merged windows the allowance can fall
   below the cost of one line. Coder self-flagged #3 ("if `budget < len(windows)` the
   allowance truncates to 0 and all context drops"). Is that reachable for a legal
   search (<= 3 hits per file, `preview_lines` 40)? Can a window end up with an
   allowance so small it emits a **partial** window asymmetrically (lines above the
   hit but not below, or vice versa)? Is partial-window emission correct?

3. **Trim-from-`sliced` correctness.** The shrink loop now filters an
   already-read dict instead of re-reading. Verify that for every radius the trimmed
   render is **byte-identical** to a fresh render at that radius. Attack the merge
   logic: `_merged_line_windows` merges overlapping AND touching windows; if two
   windows merge, does per-window allowance accounting change the result versus the
   unmerged case? Check hits at file boundaries (line 1, last line) and hits whose
   windows would overlap after shrinking.

4. **Does the budget still honour the 40-match-line cap and the guarantee
   asymmetry?** Match lines are non-negotiable at the per-file level now. Confirm:
   with 40 hits in one file, are all 40 emitted even when their cost exceeds
   `SEARCH_FILE_CHARS`? If `plain` alone exceeds the file budget, does the code
   return `plain` (correct) or shrink it (wrong)? Then check the same at the
   **aggregate** level: if 20 files each have `plain` over budget, match lines still
   emit and the total blows the cap — is that reachable, and is it the accepted
   pre-existing limitation or a new one?

5. **The two new tests — do they discriminate?** For each of the 4 Phase-2 tests,
   confirm it FAILS on the Phase-1 tree and passes now. Re-run your M4 and M11
   mutants against the new suite: are they now KILLED? Try to construct a mutant that
   still survives (e.g. truncate only the *last* match line; drop match lines only
   when `plain` exceeds the aggregate rather than file budget). Note Coder changed
   test 4/5 to call `_search_files` directly rather than `execute_tool` — judge
   whether that weakens the pin by bypassing `bound_tool_output`.

6. **Prose accuracy (your BUG #5).** New description:
   `"Each file with one to three hits includes surrounding lines."` New docstring:
   `"A file is opened only when it has one to three content matches..."`. Is this
   now accurate in every case — including when the aggregate budget has been
   exhausted and radius is forced to 0? A file with 1-3 hits may now receive **no**
   context because earlier files spent the budget. Is the description therefore
   still making a promise the code does not keep? If so, what is the minimal honest
   wording?

7. **Scope discipline.** Diff must touch ONLY `agent/tools.py` and
   `tests/test_tools.py`. Unchanged: `read_file`, `edit_file`, `_run_grep`,
   `_context_slice`, `_cut_search_line`, `_merged_line_windows`, `bound_tool_output`.
   The only new constant is `SEARCH_TOTAL_CHARS`. `MAX_READ_LINES` still untouched.
   No total-output bounding inside `bound_tool_output` itself.

---

## Deliverable

BUG #[N] per finding with severity. Explicit "no bugs found" per clean section.
One probe per section minimum. Do not fix code, do not commit, do not decide
completion. Report to the Supervisor.
