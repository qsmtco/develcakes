# Bugfix B3 — cap-then-filter in `_find_matching_files` reports false "(no matches)" (audit BUG #3, MEDIUM)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, post-commit audit round
**Basis:** Debugger adversarial audit 2026-10-16, BUG #3
**Files in scope (exactly two):** `agent/tools.py` (`_search_files` and/or
`_find_matching_files` in `agent/context.py`), plus one test.
**Supervisor verification pending** (repro from audit report).

## The bug

`_find_matching_files` (agent/context.py) caps candidates at `max_files`
INSIDE the walk (early return), and `_search_files` applies `file_type` /
search-root filters AFTERWARDS (agent/tools.py). A name-fragment query where
the top-N by size are the wrong type gets everything filtered away — the
tool reports `(no matches)` for a query that has matches.

Audit repro: 25 × 5KB `.txt` files named `big*` + one 2-byte `wanted.py`;
`_search_files('i', dir, file_type='py')` → `(no matches)` even though
`wanted.py` matches.

Pre-existing in the old `_file_search`, but the item-3 union propagated it
to `search_files`, which used to be pure grep — a regression class for
`search_files` users.

## Fix requirements

1. Filter BEFORE capping: pass `file_type` (and effective search root) into
   the matching walk (or apply the filter to candidates before the
   `max_files` truncation), so the cap counts only surviving candidates.
2. Preserve existing behaviors: sandbox enforcement, timeout, gitignore
   pruning, ordering by size, `max_results` semantics at the output layer.
3. ONE test in `tests/test_tools.py`: fixture with N `.txt` files larger
   (by size) than one `.py` file whose name matches the query; query with
   `file_type='py'` returns the `.py` file, not `(no matches)`.
4. Touch nothing else.

## Rules

- `prompts/steelFramedCodeWriter.md` applies — read before write.
- py_compile: `.venv/bin/python -m py_compile agent/tools.py agent/context.py`
- Do NOT run pytest — supervisor runs the suite.
- Deliver: COMPLETENESS checklist + the repro's before/after output.
