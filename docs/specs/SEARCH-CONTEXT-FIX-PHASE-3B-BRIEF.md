# Phase 3b: dynamic file cap from measured header cost

## Problem (Supervisor-measured, do not re-derive the whole thing)
At deep paths, `_search_files` lists N files whose headers alone overflow CONV_OUTPUT_CAP.
Each listed file costs `header + match line`, both embedding the project-relative path
(prefix appears in header AND in each context line).

Measured per-file minimum cost vs depth (20 files, one hit each, shipped layer):

| depth | rel_len | per-file min | files that fit | N=20 outcome |
|---|---|---|---|---|
| 1 | 257 | 290 | 56 | 20/20 ok |
| 2 | 508 | 541 | 30 | 20/20 ok |
| 4 | 1010 | 1043 | 15 | 9/20 OMITTED |
| 5 | 1261 | 1294 | 12 | 8/20 OMITTED |
| 6 | 1512 | 1545 | 10 | 7/20 OMITTED |
| 8 | 2014 | 2047 | 8 | 4/20 OMITTED |

A static constant cannot fix this: MAX_SEARCH_FILES=8 would be safe at depth 8 but
throws away 48 reportable files at depth 1 (56 fit). So the count must be DERIVED
from the header costs already computed, not from a fixed number.

## Change (agent/tools.py only, plus tests)
KEEP `MAX_SEARCH_FILES = 20` as the hard ceiling. It still stops `max_results=200`.

ADD a second dynamic bound inside `_search_files`, after the plan/header_costs loop
(around lines 1541-1560) and BEFORE the emission loop:

1. Per-file minimum cost for each planned entry:
   - `header_costs[i]` (already computed, includes its newline)
   - plus `0` when `count_only` is true OR the entry is name-only (no match lines emitted)
   - plus a small match-line allowance constant when it has content hits.
     Add a module constant near the others, e.g. `SEARCH_MATCH_LINE_ALLOWANCE = 120`.
2. `per_file_min = max(...)` across planned entries (conservative: use the largest).
   If `plan` is empty, skip all of this.
3. Budget available: `SEARCH_OUTPUT_CHARS - tail_reserve - len(summary_line) - 1`.
4. `affordable = max(1, min(len(plan), budget // per_file_min))`
   (guard `per_file_min <= 0` -> don't slice).
5. Slice `plan` and `header_costs` to `affordable` entries.
6. RECOMPUTE `unlisted` and the summary line AFTER slicing. Today the summary is
   built at line 1532 before the plan; it must move after the slice so
   `(+N more files not listed)` reflects the dynamic count. Keep the exact
   `[N matches in M files] (+K more files not listed)` wording.

Do NOT weaken the existing `block_budget` logic (lines 1581-1589) - it is correct and
now pinned by test_search_joined_output_budget_is_enforced. This change is additive.

## Acceptance (Supervisor will verify with a mutation)
- depth-4 fixture (4 x 250-char segments), 20 files, 1 hit each:
  no "chars omitted", len <= 16*1024, and EVERY listed file's match line present.
  Expect ~13 files listed, 13/13 match lines, len ~13659, summary says +7 more.
- depth-1 and depth-2 fixtures: still list all 20 files (56/30 fit), 20/20 match lines.
- depth-8 fixture: ~6 files, 6/6 match lines, no omission.
- `count_only=True` at depth 4: headers also respect the dynamic cap, no omission.
- `SEARCH_MATCH_LINE_ALLOWANCE` must actually BE used - a test that fails if the
  dynamic slice is removed entirely. Prove it: revert the slice locally, confirm
  the new test goes red, then restore.

## Constraints
- Files: `agent/tools.py`, `tests/test_tools.py`. Nothing else.
- Do not modify or delete any pre-existing test.
- Do not touch the 3 TestToolSchemaSizeBudget failures (pre-existing).
- ruff: agent/tools.py must stay at 18 findings (HEAD parity). tests/test_tools.py at 2.
- No temp files left in tests/. Clean up your own scratch files with truncation
  (rm is blocked for you); report any you cannot remove.

## Report back
Files + line numbers changed, measured table (depth 1/2/4/8 before vs after),
the mutation check result, both suites' pytest output, and ruff counts.
