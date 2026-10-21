# AUDIT BRIEF — search_files context repair (uncommitted working tree)

**Auditor:** Debugger. Load `.crabcakes/prompts/adversarialDebugger.md` fresh and work
through all 11 sections. Report every finding in BUG #[N] format
(Severity / Assumption violated / Attack vector / Reproduction / Root cause / Fix).
If a section comes back clean, say so explicitly — silence is not acceptance.

**Code scope (uncommitted, on top of `c0ffc179`):**
- `agent/tools.py` — `SEARCH_FILE_CHARS` (~:288), `_emit_search_hits` (~:1311-1350),
  `_search_files` per-file radius block (~:1498-1529)
- `tests/test_tools.py` — 4 new tests in `TestSearchFiles` (~:1247-1300)

**Phase contract:** `docs/specs/SEARCH-CONTEXT-FIX-PHASE-1-INSTRUCTIONS.md`
Read it first. It is the authority for this phase — there is no spec.

---

## What the phase set out to fix

**Bug A** — one hit produced a radius-50 block (up to 101 lines); the block exceeded
`CONV_OUTPUT_CAP` (16 KB), `bound_tool_output` kept head 2 KB + tail 8 KB, and the
match line in the middle was deleted. Caller saw counts and a footer pointing at a
line number that was never printed.

**Bug B** — `content_hits` summed hits across every file in the search, then one
`auto_radius` was applied to all files. Adding an unrelated 4th file silently removed
context from the other three.

---

## Supervisor verification already done (do not redo; probe past it)

- Fix B: `grep -n "content_hits\|auto_radius" agent/tools.py` -> 0 matches.
- Fix B behavior: 1 / 3 / 4 files with one hit each -> 99 / 297 / 396 context lines.
  Pre-fix the 4-file case was 0.
- Fix A behavior: original defect fixture (300 lines, 500-char lines, hit at 150)
  -> 3296 chars, `Line 150: THE_HIT` present, no omission marker, 6 context lines.
- Suites: `test_tools.py` + `test_jit_context_discovery.py` -> 185 passed,
  3 failed. The 3 are the pre-existing `TestToolSchemaSizeBudget` trio
  (6910 / 6475 / 6455 vs 4000), over budget at baseline before this work.
- ruff against this tree is **identical** to baseline `c0ffc179` (same 20 findings,
  same rule multiset). The builder could not run ruff (shell whitelist); the
  Supervisor ran it. No lint regression.
- `tests/test_tools.py` diff is 68 insertions / 0 deletions — no existing test edited.

---

## Six things to attack (plus your own 11 sections)

1. **`_emit_search_hits` termination and cost.** It is now a `while True:` loop that
   decrements `radius` by 1 and re-renders. Each iteration calls `_context_slice`
   **per merged window**, and `_context_slice` opens the file and seeks. Worst case
   is radius 50 -> 0 = 51 iterations, i.e. up to 51 file opens for ONE file with
   one hit. Is the loop guaranteed to terminate? What is the worst-case read count?
   Is that acceptable in a hot path (one search can emit up to 20 files)? Propose
   a better shape if you find one (e.g. compute both renders once, or binary search,
   or reuse `sliced` across attempts).

2. **The "match lines are never dropped" guarantee is conditional.** With 40+ hits
   whose *match text* is long (~500 chars each), `plain` alone is ~20 KB — over
   `CONV_OUTPUT_CAP` — so `bound_tool_output` still deletes middle match lines.
   Supervisor measured: 10271 chars, 19 of 40 match lines, omission marker present.
   This is IDENTICAL at baseline `58464525`, so it is pre-existing and out of scope.
   Confirm that reproduction and confirm it is unchanged by this phase. State
   plainly whether the phase's guarantee is therefore narrower than its commit claim.

3. **Test 4 does not match the contract.** The contract asked for "40+ hits in a
   file of 500-char lines". The builder implemented 3 wide hits and claimed in its
   report that the literal fixture is "unsatisfiable here". **The Supervisor has
   measured that the literal fixture PASSES post-fix**: 40 short hit lines with
   500-char neighbor lines -> 846 chars, all 40 match lines, no omission. So the
   builder's justification for the deviation is factually wrong. Judge: does the
   shipped test still pin the intended guarantee, or is the pin now weaker than
   required? Is the deviation acceptable on substance, and is the stated reason
   correct?

4. **Discrimination.** For each of the 4 new tests, state whether it FAILS on the
   pre-fix code (`git stash` the two files, or read the diff and reason it out) and
   passes post-fix. A test that cannot fail is not a pin. Pay attention to test 2
   (`..._context_is_bounded`): does it assert anything that would catch a mutant
   that returns `plain` unconditionally when the block is oversized?

5. **Budget interaction with the 40-line match cap and headers.** Confirm the
   per-file 40-match cap, the `_match_count_header` "N of M matches" format, the
   `_search_summary` first line, the footer, `count_only`, and name-only files all
   behave exactly as before. `SEARCH_FILE_CHARS` bounds the hit block only — is the
   header/footer/summary accounting still correct when the block shrinks to `plain`?

6. **Scope discipline.** Diff must touch ONLY `agent/tools.py` and
   `tests/test_tools.py`. No edits to `read_file`, `edit_file`, `_run_grep`,
   `_context_slice`, `_cut_search_line`, `_merged_line_windows`, `_ALREADY_WINDOWED`,
   `CONV_OUTPUT_CAP/HEAD/TAIL`, or `bound_tool_output`. `MAX_READ_LINES` must remain
   untouched (dead but out of scope). Total-output bounding must NOT have been added.

---

## Deliverable

BUG #[N] format for every finding, severity-tagged. Explicit "no bugs found" per
section if clean. One probe per section minimum. You do not fix code, do not commit,
do not decide phase completion — report to the Supervisor, who routes fixes.
