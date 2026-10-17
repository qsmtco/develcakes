# Bugfix C2 — `_FILE_CONTEXT_CACHE` key must include the tree fingerprint (audit BUG #1, HIGH)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, B-round re-audit
**Basis:** Debugger re-audit 2026-10-17, BUG #1 (supervisor-verified)
**Files in scope (exactly two):** `agent/context.py`, one test in
`tests/test_context.py`.

## The bug (supervisor-verified probe)

B2 fixed the system-prompt fingerprint but not the cache it feeds.
`build_file_context` (preload mode) is served from `_FILE_CONTEXT_CACHE`
keyed on `(project_path, max_chars)` with validity `cached[0] >= mtime`
where mtime = `_project_root_mtime` — root + 4 fixed files only.
A subdirectory add (agent/, ui/, utils/ — where write_file puts output)
leaves that key unchanged, so the rebuild triggered by B2 re-enters
`build_file_context` and gets **byte-identical stale text forever**.

Probe (supervisor): subdir add → turn2 missing `brand_new_module`,
`t1 == t2` byte-identical. Hybrid and JIT are unaffected (they don't use
this cache path).

Debugger's e2e: preload rebuilt prompt lacks the new file; hybrid/jit
rebuilt prompts have it.

## Fix

1. Include `_project_tree_fingerprint(project_path)` in the cache
   validation: store it in the cached tuple and compare, per the audit's
   sketch —
   - read: `if cached and cached[0] >= mtime and cached[2] == tree_fp`
   - write (BOTH sites, `:582` and `:599`): store `(mtime, text, tree_fp)`
2. Keep the existing `cache_key = f"{project_path}::{max_chars}"` string
   key shape — do not restructure the dict; only the stored tuple grows a
   third element.
3. Compute `tree_fp` once per call, next to the existing mtime read.
4. ONE test in `tests/test_context.py`: preload-mode repro — fixture,
   `build_file_context(d, max_chars=...)` twice with a subdir add in
   between; assert the second call's output contains the new file name and
   is not byte-identical to the first. (This also closes the audit's
   test-gap note: assert on content, not on the fingerprint tuple.)
5. Touch nothing else. Query-mode path untouched (it never reads this
   cache).

## Rules

- `prompts/steelFramedCodeWriter.md` applies — read before write.
- py_compile: `.venv/bin/python -m py_compile agent/context.py tests/test_context.py`
- No pytest — supervisor runs the suite.
- Deliver COMPLETENESS checklist + before/after repro output.
