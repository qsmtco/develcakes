# Bugfix B2 — fingerprint blind to new/deleted files in subdirectories (audit BUG #1, HIGH)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, post-commit audit round
**Basis:** Debugger adversarial audit 2026-10-16, BUG #1
**Files in scope (exactly two):** `agent/context.py`, `tests/test_spec23_context_working_set.py`
**Supervisor probes confirming:** new file in `pkg/` → mtime unchanged, index
changed; delete → same blindness; root-dir file → covered.

## The bug

`_system_prompt_fingerprint` uses `_project_root_mtime` as its file-context
slot. That function is `max(stat(project_root).st_mtime, 4 fixed files,
.crabcakes/prompts/*)` — a directory mtime advances only on direct-entry
add/remove, and only the ROOT is stat'd. Any change inside a subdirectory
(all code lives in `agent/`, `ui/`, `utils/` — where `write_file`/`edit_file`
put output) is invisible. Item 5 made the prompt depend on
`build_file_index` (which walks subdirectories), so fingerprint and prompt
content are now out of sync: an agent writes a new module, next turn the
prompt is NOT rebuilt, and the index the model was given does not list the
file it just created.

Debugger's chosen fix: **(b) add a cheap recursive walk-count/namelist hash
to the fingerprint.** Option (a) (declare the index immutable for the
session) contradicts the audit's own reproduction — the whole point of item
5 is that a changed project rebuilds the prompt.

## Fix requirements

1. **New helper in `agent/context.py`:** `_project_tree_fingerprint(project_path)
   -> tuple` — counts of files per top-level directory plus total file count,
   from a single `os.walk` that prunes the same dirs `build_file_index` prunes
   (`skip_dirs` set: `__pycache__`, `node_modules`, `.pytest_cache`,
   `.mypy_cache`, `.tox`, plus `EXCLUDED_DIRS` when `_include_docs()` is
   False). Stat-free: `os.walk` yields names, no `stat` calls needed. Return
   something hashable-stable (e.g. `(total_count, tuple(sorted((dir, count)
   pairs)))`).
2. **Wire into `_system_prompt_fingerprint`** (`agent/runtime.py` — this file
   joins scope): replace the `file_mtime` slot's `_project_root_mtime` value
   with `_project_root_mtime(project) + _project_tree_fingerprint(project)`
   combined (keep BOTH: root mtime catches content edits to fixed files,
   tree fingerprint catches add/remove in subdirs). Keep the else-branch
   (no project) shape.
3. **Cost bound:** the walk is the same order as `build_file_index`'s walk.
   On this repo that is well under the ~0.4s awareness cost the spec already
   tolerates per turn. Do not add caching in this fix.
4. **ONE test** in `tests/test_spec23_context_working_set.py`: create a
   fixture project, snapshot the fingerprint twice with no change (equal),
   write a NEW file into a subdirectory (fingerprint changes), delete it
   (changes back or to a third value). Use the public path: two calls to
   `runtime._system_prompt_fingerprint(conv)` with `conv.project_path`
   pointing at the fixture.
5. Touch nothing else. `_FILE_CONTEXT_CACHE`'s same blind spot is REAL but
   pre-existing and out of scope here — do not touch it (flag for a later
   chunk).

## Rules

- `prompts/steelFramedCodeWriter.md` applies — read before write.
- py_compile both files: `.venv/bin/python -m py_compile agent/context.py agent/runtime.py`
- Do NOT run pytest — supervisor runs the suite.
- Deliver: COMPLETENESS checklist, helper code, test evidence.
