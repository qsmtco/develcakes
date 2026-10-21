# PHASE 1 of 1 — search_files context: keep the match line, scope the radius per file

**No spec.** This phase runs against the code as shipped. Authority is `ARCHITECTURE.md`
plus the tool's own docstrings and the unchanged behavior of every existing test.
Do not invent a spec. Do not reference SPEC-44; it is retired.

**Files to change (exactly two):**
1. `agent/tools.py`
2. `tests/test_tools.py`

**Scope note (decided, do not revisit):** this phase fixes the two confirmed
defects only — the deleted match line, and the search-wide radius. Bounding the
TOTAL output of a many-file search (up to 20 groups can still exceed
`CONV_OUTPUT_CAP` and drop middle groups) is **out of scope**. It is pre-existing
at `58464525` and is logged as a follow-up, not fixed here. Do not add total-output
bounding.

---

## Bug A — a single hit's context block deletes the match line

`_emit_search_hits` (`agent/tools.py:1307`) builds up to 101 lines for one hit
(radius `SEARCH_CONTEXT_ONE = 50` either side). `_search_files` joins them
(`:1528`), then `execute_tool` calls `bound_tool_output` (`:2327`), which keeps
`CONV_OUTPUT_HEAD` (2 KB) + `CONV_OUTPUT_TAIL` (8 KB) and **drops the middle**
(`:2181`). The match line sits in the middle, so it is deleted.

Observed today, one hit in a 300-line file of ~500-char lines:

```
[1 matches in 1 files]
wide.py (142KB, 1 matches)
  wide.py-100:wwwww...        <- context lines survive
  ...
[... 40173 chars omitted ...] <- the "  Line 150: THE_HIT" line was here
[Open with read_file line=N only to edit.]
```

The caller gets a count and a footer telling it to "open with read_file line=N"
for a line number that was never printed. At `58464525` the same fixture returned
`  Line 150: THE_HIT` correctly (76 chars, no context block).

**Confirmed trigger:** line width >= ~150 chars, hit position not near the file
start or end. Wide-but-not-500 lines fail; at 500 chars the hit only survives if
it lands in the 2 KB head or 8 KB tail.

### Fix A — budget the block, and make the match lines non-negotiable

Add one constant next to the other search constants (`:283`):

```python
# Per-file ceiling for one emitted hit block (matches + context). A single
# file must never be able to fill the conversation cap, or bound_tool_output
# drops the middle of the block, which is where the match lines are.
SEARCH_FILE_CHARS = 4_000
```

In `_emit_search_hits`, keep the interleaved format, but **shrink `radius`
until the rendered block fits the budget.** `radius = 0` is the floor and is
always accepted:

```python
def _emit_search_hits(
    rel: str,
    full_path: str,
    previewed: list[tuple[int, str]],
    radius: int,
    budget: int = SEARCH_FILE_CHARS,
) -> list[str]:
```

Structure it as: build `match_text` and `plain` exactly as today; then try the
requested radius, and if the rendered block exceeds `budget`, retry at a smaller
radius, down to 0. Return the first rendering that fits; `plain` always fits
because the match lines are mandatory.

Requirements:
- **Match lines are never dropped to make room for context.** If even `plain`
  exceeds `budget`, return `plain` unchanged (do not truncate match lines; the
  40-line `preview_lines` cap already bounds them).
- The size check is on the **rendered strings** (`"\n".join(block)` length),
  not on a line count — a 500-char line costs far more than a 20-char one.
- Keep the existing behavior when the file cannot be read: `_context_slice`
  returns `None` -> return `plain` (unchanged).
- Do not change `SEARCH_CONTEXT_ONE` / `SEARCH_CONTEXT_FEW` values.
- Keep `_cut_search_line` and `_context_slice` as they are.

**Proof A is fixed:** one hit in a 300-line file of 500-char lines must contain
`Line <n>: <hit text>` in the returned output, and the output must stay under
`CONV_OUTPUT_CAP` (16 KB) so `bound_tool_output` does not fire.

## Bug B — the context radius is decided from a search-wide count, not per file

`_search_files:1491-1497`:

```python
content_hits = sum(len(v) for v in grep_hits.values())   # EVERY file in the search
if count_only or content_hits <= 0 or content_hits > 3:
    auto_radius = 0
elif content_hits == 1:
    auto_radius = SEARCH_CONTEXT_ONE
else:
    auto_radius = SEARCH_CONTEXT_FEW
```

One `auto_radius` is then passed to every file (`:1520`). Measured consequence:

| Search shape | Context lines emitted |
|---|---|
| 1 file, 1 hit | 99 |
| 3 files, 1 hit each | 90 |
| 4 files, 1 hit each | **0** |

Adding an unrelated fourth file silently removes context from the first three.
The tool description says "One to three hits include surrounding lines", which
reads as a per-file property.

### Fix B — decide the radius inside the per-file loop

Delete the search-wide `auto_radius` block. Inside the `for rel in ordered:` loop
(`:1500`), after `hits`/`total` are known, decide from **that file's** previewed
hit count and pass it to `_emit_search_hits`:

```python
previewed = hits[:preview_lines]
if count_only or not previewed or len(previewed) > 3:
    radius = 0
elif len(previewed) == 1:
    radius = SEARCH_CONTEXT_ONE
else:
    radius = SEARCH_CONTEXT_FEW
```

Use `previewed` (already sliced by `preview_lines`) for the decision, and pass the
same slice to `_emit_search_hits`. Do not introduce a second slice.

**Proof B is fixed:** three files each with one hit AND four files each with one
hit must both give every file context lines.

## Keep unchanged

- `count_only` -> no body lines, no footer.
- The per-file 40-match-line cap (`hits[:preview_lines]`, `preview_lines` clamped
  to 40) and the header `_match_count_header` format, including "N of M matches".
- The first-line summary `_search_summary` and the footer
  `[Open with read_file line=N only to edit.]`.
- Name-only files: `[name match only — use read_file for content]`.
- `MAX_EXEC_OUTPUT` (100 KB) truncation at `:1529`.
- `read_file`, `edit_file`, `_run_grep`, `_context_slice`, `_cut_search_line`,
  `_merged_line_windows`: **no changes.**
- Do not touch `_ALREADY_WINDOWED`, `CONV_OUTPUT_CAP/HEAD/TAIL`, or
  `bound_tool_output`.
- `MAX_READ_LINES` (`:278`) is dead but is **out of scope for this phase** —
  leave it. Report it in the COMPLETENESS checklist as "related issue, not fixed".

## Tests to add in `tests/test_tools.py` (class `TestSearchFiles`)

Each must fail on the pre-fix code. Say in a comment which bug it pins.

1. `test_search_wide_lines_keep_the_match_line` — 300 lines, ~500 chars each, one
   hit at line 150. Assert success, assert `Line 150: ` present, assert the hit
   text present, assert `len(r.output) <= 16 * 1024`.
2. `test_search_wide_lines_context_is_bounded` — same fixture; assert context
   lines ARE emitted (so the block is not just the bare match) and the block
   respects `SEARCH_FILE_CHARS`-scale bounding, i.e. `len(r.output)` is far below
   the 16 KB cap.
3. `test_search_context_radius_is_per_file` — four files, one hit each. Assert all
   four files emit at least one context line. This is the direct proof of Fix B
   (the old code emitted zero).
4. `test_search_match_lines_survive_many_hits_in_a_wide_file` — 40+ hits in a file
   of 500-char lines. Assert every emitted `  Line ` entry contains its match text
   and that the output is under the 16 KB cap.

Do not modify or delete any existing test. In particular leave
`test_search_one_hit_includes_neighbors_in_a_large_file`,
`test_search_four_hits_do_not_add_neighbors`, and
`test_search_emission_cap_40_on_large_file` exactly as they are — they must still
pass unmodified. If one of them fails after your change, your change is wrong;
fix the code, not the test.

## Verification you must run and paste

```
.venv/bin/python -m pytest tests/test_tools.py -q
.venv/bin/python -m pytest tests/test_jit_context_discovery.py -q
.venv/bin/python -m ruff check agent/tools.py tests/test_tools.py
```

Expected: the ONLY failures are
`TestToolSchemaSizeBudget::test_default_agent_tool_payload_under_budget[coder|debugger|supervisor]`.
Those three are pre-existing and over budget before this phase — do not fix them,
do not touch `TOOL_PAYLOAD_BUDGET`, do not trim tool descriptions to sneak under.

## Rules

- Use the steelFramedCodeWriter prompt at `.crabcakes/prompts/steelFramedCodeWriter.md`
  and apply every rule in it.
- Read each file in full before editing.
- No unrelated refactors, no reformatting, no docstring rewrites beyond what the
  changed behavior requires.
- Report any adjacent bug you notice but do not fix it — flag it as
  "related issue, not fixed in this phase".

## COMPLETENESS checklist (required in your report)

```
COMPLETENESS:
- [ ] Fix A: SEARCH_FILE_CHARS added; _emit_search_hits budgets and shrinks radius — evidence
- [ ] Fix A: match lines never truncated to fit — evidence
- [ ] Fix B: search-wide auto_radius deleted; radius decided per file — evidence (grep for content_hits)
- [ ] Test 1 wide-lines match visibility — passes, and fails pre-fix
- [ ] Test 2 wide-lines bounded context — passes
- [ ] Test 3 per-file radius (4 files) — passes, fails pre-fix
- [ ] Test 4 many hits in wide file — passes
- [ ] No existing test modified or deleted — evidence (git diff --stat)
- [ ] pytest output pasted (test_tools.py + test_jit_context_discovery.py)
- [ ] ruff output pasted
- [ ] Files changed list with line numbers
- [ ] Related issues found but NOT fixed
```
