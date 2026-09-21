# Debugger — Phase 1 Adversarial Audit Request

**Scope:** Phase 1 of `docs/specs/SPEC-FILE-TREE-ENHANCEMENTS.md`
**Files to audit:**
- `ui/views/file_tree.py` (changes to `FileTreeRow`, `FileTreeRowWidget`, `FileTreeFactory._on_bind`, `_show_tree`, `_expand_directory`, `_on_directory_loaded`, + module-level helpers `format_size`/`format_mtime`/`git_status_to_display`)
- `tests/test_projects.py` (3-tuple → 5-tuple fixes)
- `tests/test_git_ops.py` (new `TestStatusPorcelain` class)
- `ui/styles.py` (new CSS classes)

**My (Supervisor) verification already found 2 bugs — confirm or refute them, then look for more:**

1. **BUG A-1 (bug):** Duplicate `TestStatusPorcelain` class name in `test_git_ops.py` (line 263 pre-existing for `status()`, line 639 new for `status_porcelain()`). Python shadows the first → `test_status_new_file` silently dropped from collection. Confirm via `pytest tests/test_git_ops.py --co -q | grep test_status_new_file` (returns nothing = bug confirmed).

2. **BUG A-2 (issue):** `test_too_short_line_skipped` doesn't actually test a too-short line (asserts on a clean repo). `test_worktree_rename_both_status_positions` stages an index rename, not a worktree rename — so the `status_code[1] in ('R','C')` branch is untested.

**Your job:** Load `prompts/adversarialDebugger.md` fresh. Work through all 11 sections of your adversarial probe on the Phase 1 diff. Focus areas:
- Does the icon binding actually work end-to-end? (is `icon_name` ever stale?)
- Are there off-by-one or precision bugs in `format_size` / `format_mtime`?
- Does `git_status_to_display` handle all porcelain edge cases (deleted in worktree `_D`, untracked `??`, ignored `!!`)?
- Is the `mtime_ns // 1_000_000_000` integer division correct at all boundaries (epoch, future dates, 0)?
- Any GObject property default value issues (the `—` em-dash vs `--` hyphen)?
- Does `_show_tree` still correctly populate `has_children` for directories?

Full supervisor audit notes are in `docs/specs/file-tree-phases/PHASE-1-SUPERVISOR-AUDIT.md` — read it.

Report bugs in the `## Audit Report` format with `**Pattern:**` tags. Do NOT fix code — only report.
