# Debugger — Consolidated Adversarial Audit: P1 Fixes + P2 + P2 Fixes

**Scope:** Everything since your last Phase 1 audit. Three batches of changes:
1. Phase 1 audit fixes (5 bugs: format_mtime future guard, class rename, method rename, 2 test rewrites, parent_full_path wiring)
2. Phase 2 (3 new factories, 4-column layout, git status stub)
3. Phase 2 audit fixes (3 bugs: missing import os, child-row git status, search dispatcher guard)

**Files changed (current state on disk):**
- `ui/views/file_tree.py` — 3 new factory classes (FileTreeStatusFactory, FileTreeSizeFactory, FileTreeModifiedFactory), `_show_tree` now builds 4 columns + queries git status stub, `_show_project_picker` resets to single column, `import os` added, `_git_status_map` stored on self and used in `_on_directory_loaded`, `_on_search_changed` has tree-mode guard, `format_mtime` future guard, `parent_full_path` populated on drawer rows
- `tests/test_git_ops.py` — class renamed (TestStatusPorcelain → TestStatusPorcelainFn), method renamed (test_count_clamping → test_count_clamping_with_line_counts), 2 tests rewritten with monkeypatch
- `tests/test_file_tree_helpers.py` — NEW, 32 tests for format_size/format_mtime/git_status_to_display

**My verification status:** 137/137 tests pass. All reported fixes independently confirmed by grep + import checks.

**Your job:** Load `prompts/adversarialDebugger.md` fresh. Run your full 11-section probe. I've already found and fixed 8 bugs across these batches — confirm the fixes hold AND look for new issues I missed.

**Focus areas:**
- Factory `_on_bind`/`_on_unbind` lifecycle — do the CSS classes leak across row recycling? (ColumnView recycles widgets; `_on_unbind` is currently a no-op `pass` in all 3 new factories)
- `_init_sort_filter` will be added in Phase 3 — is there anything in the current `_show_tree`/`_clear_all_state` that will conflict with the sort/filter model chain being layered on top?
- `format_mtime` future guard — does `diff.days < 0` catch ALL future cases? What about a timestamp 1 second in the future (diff.days == 0, diff.seconds negative)?
- `parent_full_path` is set on drawer rows — but is it ever read yet? (It shouldn't be consumed until Phase 3's `_filter_func`)
- `_git_status_map` — is it correctly cleared on `_clear_all_state`? Does it survive a project switch without leaking the old project's status into the new one?
- The git status lookup uses `os.path.relpath(full_path, path)` — does this match the keys that `status_porcelain` emits? (status_porcelain emits repo-relative paths with forward slashes; os.path.relpath on Linux also uses forward slashes — but verify)
- `_on_search_changed` tree-mode guard returns silently — is that the right behavior, or should it clear the search query?

Report bugs in the `## Audit Report` format with `**Pattern:**` tags. Do NOT fix code — only report.
