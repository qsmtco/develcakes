# Bugfix C4 — fold a name-hash into the tree fingerprint so renames are caught (audit BUG #2, LOW)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, B-round re-audit
**Basis:** Debugger re-audit 2026-10-17, BUG #2
**Files in scope (exactly two):** `agent/context.py`
(`_project_tree_fingerprint`), one test in
`tests/test_spec23_context_working_set.py`.

## The bug (audit-verified)

The fingerprint counts files per top-level directory. A rename within a
directory (`pkg/a.py` → `pkg/b.py`) keeps counts unchanged — but
`build_file_index` output DOES change (it lists names). The fingerprint
misses a real prompt change. A size-changing edit is also missed while
the index changes; a same-size edit is correctly missed (index unchanged
too).

## Fix (audit's sketch)

1. In `_project_tree_fingerprint`: collect a rolling hash of the walked
   relative paths — `h = hashlib.blake2b(digest_size=8)`,
   `h.update(rel_path.encode("utf-8", "replace"))` per file — and return
   `(total, tuple(sorted(per_top.items())), h.hexdigest())`.
   Still stat-free (names only), same walk, same prune set.
2. Import `hashlib` at module top (stdlib, allowed).
3. ONE test: fixture, fingerprint A; rename `pkg/a.py` → `pkg/b.py`;
   fingerprint B; assert A != B. Keep the existing
   `test_fingerprint_catches_subdir_add_and_delete` untouched.
4. Touch nothing else. Do NOT stat files (size edits stay out of scope —
   the index only changes on size change, and stat-ing every file defeats
   the 1.6ms cost that made this fix viable).

## Rules

- `prompts/steelFramedCodeWriter.md` applies. py_compile touched files.
- No pytest. Deliver COMPLETENESS checklist.
