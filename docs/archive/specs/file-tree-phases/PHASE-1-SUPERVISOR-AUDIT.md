# Phase 1 Audit Notes — Supervisor verification of Coder's delivery

**Date:** 2026-07-21
**Scope:** Phase 1 of SPEC-FILE-TREE-ENHANCEMENTS

## Independent verification performed

1. Re-ran all 3 target test files: 103/103 pass. ✅
2. `git diff --stat` — all 4 files modified as claimed. ✅
3. grep'd for stale 3-tuple unpacking in `file_tree.py` → 0 matches. ✅
4. Full-repo grep for `scan_directory` callers → only `file_tree.py` (2 sites), both 5-tuple. ✅ (applied partial-grep lesson)
5. Verified all 9 new GObject properties present (lines 106-115). ✅
6. Verified `set_icon(icon_name, is_dir, is_drawer)` + `set_icon_color` + `_on_bind` wiring. ✅
7. Verified CSS classes added (3 grep matches). ✅
8. Verified `format_size(1500)` → "1.5 KB", `format_mtime(0)` → "—", `git_status_to_display('M ')` → "M". ✅

## Bugs found (2)

### BUG A-1 — Duplicate class name shadows pre-existing TestStatusPorcelain (test collection loss)
- **Severity:** bug
- **File:** tests/test_git_ops.py:263 and :639
- **Bug:** Two classes named `TestStatusPorcelain`. Python/pytest silently shadows the first (line 263, which tests the old `status()` function with `test_status_new_file`) with the second (line 639, the new `status_porcelain` tests). The pre-existing `test_status_new_file` is **dropped from collection** — confirmed via `pytest --co` (collects only 7 functions from the second class, 0 from the first).
- **Root cause:** Coder did not grep for existing class name before adding the new test class.
- **Fix:** Rename the new class to `TestStatusPorcelainFn` (the new tests test the `status_porcelain` function; the old tests the `status` function). Keep the old class name as-is to avoid breaking other references.
- **Pattern:** name-collision
- **Tests:** `pytest tests/test_git_ops.py --co -q | grep -c test_status_new_file` must return 1 after fix.

### BUG A-2 — Misleading test names/docstrings (not asserting what they claim)
- **Severity:** issue
- **File:** tests/test_git_ops.py:724 `test_too_short_line_skipped`, :736 `test_worktree_rename_both_status_positions`
- **Bug:** 
  - `test_too_short_line_skipped` does NOT test a too-short line — it calls `status_porcelain` on a clean repo (output "") and asserts `result == {}`. The docstring says "A line shorter than 4 chars is skipped" but no such line is ever fed to the parser. The skip logic (BUG #4 fix) is untested.
  - `test_worktree_rename_both_status_positions` docstring claims to test worktree-column rename (" R old -> new"), but the test stages an index rename (`repo.index.add`/`index.remove`), producing index-column "R " not worktree-column " R". The `status_code[1] in ('R','C')` branch (BUG #25 worktree side) is not actually exercised.
- **Fix:** 
  - For too-short-line: inject a malformed line directly by calling the parser path, OR monkeypatch `repo.git.status` to return a too-short line like `"XY\n"`, asserting it's skipped.
  - For worktree-rename: produce a true worktree rename (mv the file in the working tree WITHOUT staging the rename) so git emits " R"-style output; assert the key is present.
- **Pattern:** misleading-test-assertion

## Non-bugs (verified acceptable)

- **Segmentation faults** in `test_file_tree_columnview.py`, `test_left_panel.py`, `test_chat_render_handler.py`, `test_diff_viewer.py`, `test_gtk_safe_link.py` — **pre-existing environmental** (no display server in sandbox). Confirmed via git history (these tests were committed before Phase 1). NOT caused by Phase 1.
- `_show_tree` uses stub `git_status=""` / `git_status_display=""` — correct per Phase 1 scope (handler wiring is Phase 4).

## Verdict

Phase 1 code is correct and the app no longer crashes. BUG A-1 must be fixed (silent test loss is a coverage regression). BUG A-2 should be fixed for test integrity. Both are test-file-only changes, no production code touched.
