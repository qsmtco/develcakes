# PHASE 2 of 2 — search_files context: aggregate budget, per-window radius, cheaper shrink

**No spec.** Code as shipped. Authority: `ARCHITECTURE.md` + existing test contract.
Continues directly from `SEARCH-CONTEXT-FIX-PHASE-1-INSTRUCTIONS.md`.

**Files to change (exactly two):**
1. `agent/tools.py`
2. `tests/test_tools.py`

Phase 1 was audited by the Debugger and returned **5 bugs**. The Supervisor
independently reproduced 4 of them. This phase fixes BUGs #1-#4 and #5.
Do not re-do the Phase 1 work; it stays.

---

## BUG #1 (HIGH, regression caused by Phase 1) — the aggregate exceeds the cap

Phase 1 gave **every** file its own context radius. Each file can now emit up to
`SEARCH_FILE_CHARS` (4000) chars, but nothing bounds the **total**. Five files x
4 KB > `CONV_OUTPUT_CAP` (16384) -> `bound_tool_output` fires and deletes the middle
— which is where the **match lines** are.

Reproduced by the Supervisor on the current tree:

```python
# 20 files, 1 hit each at line 150, ~35-char lines
execute_tool("search_files", {"pattern": "HIT_"}, proj)
# phase-1 tree : 10271 chars, 3 of 20 match lines, "omitted" marker present
# c0ffc179     :   938 chars, 20 of 20 match lines
```

This is a **net regression**: the common "one hit per file across many files" search
returns fewer match lines than the code it replaces. It must not ship.

### Fix #1 — one aggregate budget across the whole search, match lines first

Add a constant next to `SEARCH_FILE_CHARS`:

```python
# Whole-search ceiling for emitted hit blocks. Bound what we emit so
# bound_tool_output never has to drop the middle (it would drop match lines).
SEARCH_TOTAL_CHARS = 12_000
```

In `_search_files`, keep a running total of the characters spent on hit blocks.
**Match lines are non-negotiable and are always emitted; only context is
sacrificed.** Concretely:

1. For each file, always emit its match lines (`plain`).
2. Track `spent` = running chars of emitted blocks.
3. Only allow context when there is room: the per-file budget passed to
   `_emit_search_hits` becomes
   `min(SEARCH_FILE_CHARS, SEARCH_TOTAL_CHARS - spent)`.
4. If the remaining room is <= 0, pass `radius=0` for that file — match lines still
   emit, context is skipped. Never skip a file, never drop a match line.
5. Add to `spent` the rendered length of the block that was actually emitted.

Requirement: with 20 files x 1 hit, **all 20 match lines must survive** and the
final output must stay under `CONV_OUTPUT_CAP`. Context will be reduced or absent
for the later files — that is the correct trade, and it is what pre-fix behavior
did (all match lines, no context).

Do **not** touch `bound_tool_output`, `CONV_OUTPUT_CAP/HEAD/TAIL`, or
`_ALREADY_WINDOWED`.

## BUG #2 (MEDIUM) — the shrink loop re-reads the file up to 51 times

`_emit_search_hits`' `while True:` renders from scratch at each radius step, and
every render calls `_context_slice` per merged window, which opens the file and
seeks from byte 0. Measured by the Supervisor: **48 `_context_slice` calls for a
single 1-hit file**; Debugger measured 960 calls / 1920 file opens for a 20-file
search (2402 MB read, 6614 ms).

### Fix #2 — read once, then shrink by trimming

Restructure so the file is read **once** at the largest radius, then the radius is
reduced by trimming the already-read `sliced` dict — a smaller window's lines are a
subset of the larger window's. Concretely: compute `sliced` for the requested
radius once, then for each candidate radius render from `sliced` (filtering to the
smaller window) without calling `_context_slice` again.

Acceptance: for one 1-hit file, `_context_slice` must be called **once per merged
window** (1 for a single hit), not up to 48 times. Termination must remain
guaranteed (radius reaches 0 and `radius == 0` returns unconditionally).

## BUG #3 (MEDIUM) — one wide window starves a narrow window in the same file

The shrink loop applies a **single** radius to all merged windows in a file. A hit
whose neighbours are long collapses the radius for a hit whose neighbours are short
— Bug B's defect at window granularity.

Reproduced by the Supervisor (`_emit_search_hits` directly, radius 15, budget 4000):
a narrow hit at line 10 plus a wide hit at line 200 -> **the narrow hit's lines 1-9
were lost**.

### Fix #3 — shrink per window, not per file

Each merged window carries its own allowance. Give every window its own radius and
its own size check, so a wide window cannot starve a narrow one. A reasonable
shape: give each window `budget // n_windows` as its allowance, render it, and
shrink only that window's radius if it overruns. Compose with Fix #2 (trim from the
already-read `sliced`), and with Fix #1 (the file-level allowance still caps the
total).

Requirement: in the two-hit fixture above, the narrow hit must keep its full radius
when its own window fits its allowance.

## BUG #4 (MEDIUM) — "match lines are never dropped" is not pinned

Two mutants survive all four Phase 1 tests because every fixture has short match
text:
- `M4` truncates each match line to `budget`.
- `M11` returns `[]` when `plain` alone exceeds the budget.

### Fix #4 — add two tests that kill M4 and M11

Add to `TestSearchFiles`:

5. `test_search_many_wide_hits_count_is_exact` — 40 hits whose **match text** is
   ~500 chars each (the match line alone blows the budget). Assert
   `r.output.count("  Line ") == the number of emitted match lines` and that each
   emitted match line retains its full text (not truncated). If the aggregate cap
   legitimately limits how many fit, assert the count equals exactly the number of
   `  Line ` entries the summary implies and that **no match line is truncated**
   (`"Line " ` entries must not be cut mid-text). Choose the assertion that M4 and
   M11 both fail — a truncated or dropped match line must be caught.
6. `test_search_wide_hit_does_not_starve_narrow_hit` — the BUG #3 fixture: one
   narrow-neighbour hit and one wide-neighbour hit in the same file. Assert the
   narrow hit's window is present.

Each must fail on the Phase 1 tree and pass after this phase.

## BUG #5 (LOW) — prose still describes per-search semantics

The model-facing description (`~:1871`) says "One to three hits include surrounding
lines", and the `_search_files` docstring (`~:1370`) says "The file is opened only
when **the search** has one to three content matches". Both now describe a per-file
rule. Reword to per-file: "Each file with one to three hits includes surrounding
lines." Keep it short — the tool descriptions are already over the payload budget
and must not grow meaningfully (do not add more than ~10 chars).

## Keep unchanged (verified clean by the audit — do not regress)

- The per-file 40-match-line cap, `_match_count_header` "N of M matches" format,
  the `_search_summary` first line, and the footer.
- `count_only` and name-only files.
- `read_file`, `edit_file`, `_run_grep`, `_context_slice`, `_cut_search_line`,
  `_merged_line_windows`, `MAX_EXEC_OUTPUT`.
- `MAX_READ_LINES` stays dead (out of scope).
- No existing test may be modified or deleted. Phase 1's four tests must still
  pass unmodified.

## Verification you must run and paste

```
.venv/bin/python -m pytest tests/test_tools.py -q
.venv/bin/python -m pytest tests/test_jit_context_discovery.py -q
```

(Ruff is not in your shell whitelist — the Supervisor runs it.)

Expected: the ONLY failures remain the three pre-existing
`TestToolSchemaSizeBudget` tests. Do not fix them, do not touch `TOOL_PAYLOAD_BUDGET`.

## Rules

- Use the steelFramedCodeWriter prompt at `.crabcakes/prompts/steelFramedCodeWriter.md`.
- Read each file in full before editing.
- Do not rewrite the phase 1 fix; extend it.
- Flag adjacent issues, do not fix them.

## COMPLETENESS checklist (required in your report)

```
COMPLETENESS:
- [ ] Fix #1: SEARCH_TOTAL_CHARS added; aggregate budget; match lines always emitted — evidence
- [ ] Fix #1: 20 files x 1 hit -> all 20 match lines, output under 16KB — evidence (paste the counts)
- [ ] Fix #2: file read once per window; _context_slice call count reduced — evidence
- [ ] Fix #3: per-window radius; narrow hit not starved — evidence
- [ ] Fix #4: test 5 and test 6 added, both fail on phase-1 tree — evidence
- [ ] Fix #5: description + docstring reworded per-file — evidence (paste the new text)
- [ ] Phase 1's four tests still pass unmodified — evidence
- [ ] No existing test modified/deleted — evidence (git diff --stat)
- [ ] pytest output pasted (both suites)
- [ ] Files changed list with line numbers
- [ ] Related issues found but NOT fixed
```
