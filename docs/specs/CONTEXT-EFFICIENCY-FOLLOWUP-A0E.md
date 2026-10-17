# Bugfix A0E — §4.4a docs must survive the forced-lose overflow regime

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP supervising loop, chunk A0e
**Basis:** Debugger re-audit BUG #7 (MEDIUM), 2026-10-16
**Files in scope (exactly two):** `agent/context.py` (build_file_context), `tests/test_context.py`
**Pre-req:** A0a–A0d already verified green in the working tree.

## The bug

`build_file_context` assembles `docs_section + prompt_block + tail` and, when the
total exceeds `max_chars`, cuts with `full[-keep:]` — a **tail cut**. The docs
section sits at the FRONT of `full`, so when prompts are already dropped/absent
and `len(docs_section) + len(tail) > max_chars`, the cut deletes the entire
`## Project docs` section, heading included, **silently** — while the tree
survives.

Measured by Debugger:
- On this repo: `build_file_context(repo, max_chars=40_000)` loses docs
  (docs measure ~36,536 chars).
- Fixture `8 docs x 7,000 chars` loses docs even at the DEFAULT 50,000 budget.
- OLD-vs-NEW at the same fixture: the pre-diff head-cut code **kept** docs in
  this same case. The tail cut is §7-mandated, but §7 is silent on docs-vs-tree;
  the architecture is not.

## Why docs must win

`docs/ARCHITECTURE.md:2073` (§4.4a): *"always include project docs in context"* —
this is an architecture-level guarantee. Per the authority hierarchy, when the
spec is silent, architecture wins. The forced-lose drop order is therefore:

1. **prompt bodies first** (A0b reserve arithmetic — unchanged)
2. **the tail (tree/key files) second**
3. **docs last** — never silently

## Fix requirements

1. **Section-aware overflow path.** When overflow occurs (len(full) > max_chars)
   AND `len(docs_section) + len(tail) > max_chars` (prompts already gone or
   unable to fit), keep `docs_section` intact and cut the **tail** to
   `max_chars - len(docs_section) - len(marker)`, then append the marker text
   **UNCHANGED** from A0c.
2. **Degenerate case.** If `len(docs_section)` alone exceeds
   `max_chars - len(marker)`, cut docs from the END of the docs section but
   PRESERVE the literal `## Project docs` heading at the start, then the marker.
   Guard the `full[-0:]` whole-string edge (`keep == 0` → emit marker alone),
   exactly as the A0c fix does.
3. **Length invariant.** The returned string must be `<= max_chars` in ALL
   paths, including the degenerate case and the boundary
   `max_chars == len(marker)`.
4. **No changes** to: the no-overflow path, the A0b prompt-reserve arithmetic
   (`prompt_budget = max_chars - len(tail) - len(docs_section) - _PROMPT_WRAP`),
   the all-or-nothing prompt-block fallback, or the marker text.
5. **Docstring.** Update `build_file_context`'s cap sentence to state the drop
   order explicitly: prompt bodies first, then tree/key files, docs last.
6. **ONE new test** in `tests/test_context.py`: fixture with oversized docs
   (e.g. 4 docs x 8,000 chars) plus source files, `max_chars` chosen so
   docs+tail overflow but docs alone fit. Assert:
   - `## Project docs` heading present
   - truncation marker present
   - `len(result) <= max_chars`
   - the tree is the cut layer (tree heading absent or partially cut)

## Rules

- `prompts/steelFramedCodeWriter.md` applies in full — read before write,
  verify every claim, touch nothing outside the two files.
- Coder does NOT run pytest. Syntax check only:
  `.venv/bin/python -m py_compile agent/context.py tests/test_context.py`
- Deliver: py_compile output pasted, fixture arithmetic in the report, and a
  COMPLETENESS checklist mapping every numbered requirement to evidence.

## Verification (supervisor-side, after delivery)

- `pytest tests/test_context.py tests/test_phase4.py -q` green
- BUG #7 reproduction no longer loses docs at 40K/50K budgets
- All previously green suites stay green (A0a–A0d tests, test_phase4.py §4.4a)
