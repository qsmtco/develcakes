# PHASE 3 of 3 — bound the JOINED output, not the blocks

**No spec.** Code as shipped. Authority: `ARCHITECTURE.md` + existing test contract.
Continues from Phase 1 and Phase 2. Phase 2 fixed all five Phase-1 audit bugs and the
re-audit confirmed those are closed. The re-audit opened 6 new/remaining findings.
This phase closes **#1, #3, #4, #5, #6 by code+wording**; **#2 is accepted as
pre-existing and is pinned, not fixed** (see below).

**Files to change (exactly two):**
1. `agent/tools.py`
2. `tests/test_tools.py`

---

## The root cause of BUG #1 (HIGH) — `spent` is the wrong quantity

`SEARCH_TOTAL_CHARS` currently bounds only the **hit blocks**. The per-file headers
(each containing a relative path), the summary line, and the footer are never
charged. So the emitted string can exceed `CONV_OUTPUT_CAP` (16384) while `spent`
sits comfortably at 12,000, and `bound_tool_output` then deletes the middle — where
the match lines are.

Supervisor repro (20 files x 1 hit at line 150, only the **path depth** varies):

| path | chars | match lines | omitted |
|---|---|---|---|
| short (13,038) | 13,038 | 20/20 | no |
| 2 x 100-char segs | **10,270** | **19/20** | **yes** |
| 4 x 250-char segs | **10,271** | **8/20** | **yes** |

Adding nesting to a search must never delete match lines.

### Fix #1 — bound `len("\n".join(lines))`

`bound_tool_output` compares the **joined output length** against
`CONV_OUTPUT_CAP`. So must we.

Add a constant next to the others:

```python
# Ceiling for the WHOLE emitted search output, in characters of the joined
# string. Sits below CONV_OUTPUT_CAP so bound_tool_output never fires and
# never has to drop the middle (it would drop match lines).
SEARCH_OUTPUT_CHARS = 14_000
```

In the `_search_files` emission loop, before appending a file's block, compute what
the assembled output would cost **including** its header, and only spend budget
that is actually left:

1. Charge the header: add `len(header) + 1` to `spent` when the header is appended.
2. Reserve the tail: hold back enough room for the summary, the blank line, and the
   footer so they are never the thing that tips it over. Compute once, before the
   loop: `tail_reserve = len("\n".join(<blank + footer>)) + 1`.
3. Per-file block budget becomes
   `min(SEARCH_FILE_CHARS, max(0, SEARCH_OUTPUT_CHARS - spent - tail_reserve))`.
4. If that is <= 0, force `radius = 0` (match lines still emit).
5. Charge the emitted block: `spent += sum(len(s) + 1 for s in block)`.

Requirement: for **every** fixture in the table above — short, deep, and very deep
paths — `len(execute_tool(...).output) <= 16 * 1024` and the `omitted` marker is
absent. Context may shrink on deep paths; **match lines must not disappear**. All
20 match lines must survive in all three path configurations.

Do not change `bound_tool_output`, `CONV_OUTPUT_CAP`, `CONV_OUTPUT_HEAD/TAIL`, or
`_ALREADY_WINDOWED`.

## BUG #3 (MEDIUM) — `file_search` lets an unbounded file count through

`file_search` forwards any `max_results` straight to `_search_files`, and
`_search_files` clamps nothing. `max_results=200` over 200 matching files:

- **`c0ffc179`: 6,533 chars, 200/200 match lines, no omission.**
- current tree: **10,271 chars, 189/200 match lines, omission marker.**

That is a regression against baseline. `file_search` is hidden from the API schema
but stays registered so stored transcripts can replay it.

### Fix #3 — clamp `max_results`

Clamp it the way `preview_lines` is already clamped (to 40). Add next to the other
limits:

```python
MAX_SEARCH_FILES = 20
```

At the top of `_search_files`, alongside the existing `preview_lines` clamp:
any `max_results` that is not an int, is a bool, or is <= 0 becomes 20; otherwise
`max_results = min(max_results, MAX_SEARCH_FILES)`.

Note the count-only path is unaffected — `shown = total` still reports the file's
true hit count.

## BUG #4 (MEDIUM) — two tests do not discriminate

Both `test_search_many_wide_hits_count_is_exact` and
`test_search_context_slice_called_once_per_window` **pass on the Phase-1 tree**, so
they pin nothing. Measured cause for the second: its fixture uses 8-char lines, so
the 101-line block fits the 4000-char budget on the first try and the Phase-1 shrink
loop never iterates (`slice_calls=1` on both trees).

### Fix #4 — make both fixtures exercise the defect

- `test_search_context_slice_called_once_per_window`: use **wide lines** (~496
  chars) so the block must actually shrink, and assert the call count is small (1
  per merged window). On the Phase-1 tree this fixture measured **48** calls, so it
  discriminates. Keep the spike on `_context_slice`.
- `test_search_many_wide_hits_count_is_exact`: add the **tool-visible** assertion
  (see BUG #6) so it observes the shipped path, and include a second file so the
  aggregate path is exercised rather than only the per-file budget.

After your change, each of these two tests must FAIL when run against the Phase-1
behavior and pass now. You do not have to build a Phase-1 tree — reason it from the
fixture: state in a comment which behavior the fixture requires and why the old code
would fail it.

## BUG #5 (LOW) — the description over-promises

`"Each file with one to three hits includes surrounding lines."` is false once the
budget is exhausted: measured 9 of 20 one-hit files receive **zero** context lines.
The docstring at `~:1389` has the same problem.

### Fix #5 — add the budget caveat

Description (keep the growth small — these schemas are already over the 4000 budget;
stay within ~25 chars of the current text):
`"Files with one to three hits include surrounding lines, budget permitting. "`

Docstring: `"A file is opened only when it has one to three content matches.
Surrounding lines are shown when the search-wide budget allows; match lines are
always shown."`

## BUG #6 (LOW) — test 5 hides the shipped behavior

The test calls `_search_files` directly and asserts 40 match lines — but through
`execute_tool` the same fixture shows only 20 because `bound_tool_output` fires.

### Fix #6 — assert both layers

Keep the direct call for the emission contract, and add the tool-visible assertion
next to it. Because the aggregate now bounds the output (Fix #1), the tool-visible
run must show the `omitted` marker absent. Assert:
- direct: match lines are never truncated (each retains its full text);
- via `execute_tool`: `"chars omitted" not in output` and the output is
  `<= 16 * 1024`.

## BUG #2 (MEDIUM) — accepted as pre-existing, pinned not fixed

20 files x 3 hits with ~490-char hit text produces ~30,518 chars; only 20 of 60
match lines reach the model. **This is identical at `c0ffc179`** and is explicitly
out of scope. Do NOT add match-line capping or file-dropping.

Instead add one test that **documents** the accepted limitation, so nobody mistakes
it for a regression:

`test_search_match_line_aggregate_limit_is_pinned` — the fixture above; assert
`_search_files` returns all 60 match lines untruncated (the per-file guarantee), and
assert that via `execute_tool` the output is bounded by `CONV_OUTPUT_CAP`. A comment
must state: per-file guarantee holds, aggregate truncation is a known accepted
limitation, identical at `c0ffc179`.

## Keep unchanged (verified clean across two audits)

- The 40-match-line cap, `_match_count_header` "N of M matches", `_search_summary`,
  the footer, `count_only`, name-only files.
- `read_file`, `edit_file`, `_run_grep`, `_context_slice`, `_cut_search_line`,
  `_merged_line_windows`, `bound_tool_output`.
- `MAX_READ_LINES` stays dead (out of scope).
- The trim-from-`sliced` implementation and the per-window allowance are CONFIRMED
  CORRECT (1,675 configurations, byte-identical). **Do not restructure them.**
- No existing test may be modified or deleted. All 8 existing search tests must
  still pass.

## Verification you must run and paste

```
.venv/bin/python -m pytest tests/test_tools.py -q
.venv/bin/python -m pytest tests/test_jit_context_discovery.py -q
```

Expected: only the three pre-existing `TestToolSchemaSizeBudget` failures.
Do not fix them, do not touch `TOOL_PAYLOAD_BUDGET`.

Do not leave junit XML or other artifacts in `tests/`.

## Rules

- Use the steelFramedCodeWriter prompt at `.crabcakes/prompts/steelFramedCodeWriter.md`.
- Read each file in full before editing.
- Extend Phase 1 and 2; do not rewrite their logic.
- Flag adjacent issues, do not fix them.

## COMPLETENESS checklist (required in your report)

```
COMPLETENESS:
- [ ] Fix #1: SEARCH_OUTPUT_CHARS added; joined output bounded; tail reserved — evidence
- [ ] Fix #1: deep-path fixtures (2x100 and 4x250) -> 20/20 match lines, no omission, <=16KB — evidence (paste all three rows)
- [ ] Fix #3: MAX_SEARCH_FILES clamp; file_search max_results=200 -> match lines >= baseline — evidence (paste counts)
- [ ] Fix #4: slice-once test now uses wide lines (Phase 1 would give ~48 calls) — evidence
- [ ] Fix #4: wide-hits test now observes the tool-visible path — evidence
- [ ] Fix #5: description + docstring carry the budget caveat — evidence (paste new text)
- [ ] Fix #6: test asserts both direct and execute_tool layers — evidence
- [ ] BUG #2: aggregate limit pinned by a documenting test, NOT fixed — evidence
- [ ] All 8 pre-existing search tests still pass unmodified — evidence
- [ ] No existing test modified/deleted — evidence (git diff --stat)
- [ ] No artifacts left in tests/ — evidence (git status)
- [ ] pytest output pasted (both suites)
- [ ] Files changed list with line numbers
- [ ] Related issues found but NOT fixed
```
