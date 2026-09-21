# Phase 2 — Supervisor Audit: 3 Bugs Found

**Date:** 2026-07-21
**Scope:** Phase 2 (3 new factories + 4-column layout + git status stub)
**Coder report:** "137 passed" — but 2 of the 3 bugs are runtime crashes hidden by the GTK test segfault in this sandbox.

---

## BUG P2-1 — Missing `import os` → NameError crash (functional, severity: bug)

**File:** `ui/views/file_tree.py:870`
**Verified:** `grep -n "^import os" ui/views/file_tree.py` → exit 1 (no match). `python3 -c "import ui.views.file_tree as ft; print('os' in dir(ft))"` → `False`.

**Bug:** `_show_tree` line 870 uses `os.path.relpath(full_path, path)` to compute the git status lookup key, but `os` is never imported in the module. This is a **hard `NameError`** the first time `_show_tree` populates rows for any project that has files.

**Why tests didn't catch it:** The GTK-based tests (`test_file_tree_columnview.py`) segfault in this sandbox (no display server) and never reach `_show_tree`. The non-GTK test suite (137 tests) doesn't instantiate the FileTree widget.

**Fix:** Add `import os` to the imports block (after `import threading`).

**Verify:** `python3 -c "import ui.views.file_tree as ft; print('os' in dir(ft))"` → `True`. Then `python3 -c "import gi; gi.require_version('Gtk','4.0'); import os; from ui.views.file_tree import FileTree; print('import OK')"`.

**Pattern:** missing-import
**Tests:** Add a non-GTK test that imports the module and checks `os` is in its namespace: `assert hasattr(ui.views.file_tree, 'os')`.

---

## BUG P2-2 — Child rows never receive git status (functional, severity: bug)

**File:** `ui/views/file_tree.py:1733-1756` (`_on_directory_loaded` child loop)

**Bug:** When a directory is expanded, `_on_directory_loaded` constructs child `FileTreeRow`s but hardcodes `git_status=""` and `git_status_display=""`. So **only root-level files show git status badges**. Any file inside a subdirectory (the common case) shows a blank Status column even if it's modified/untracked.

The git status map is fetched once in `_show_tree` (via `self._on_get_git_status()`), but it's not stored on `self`, so `_on_directory_loaded` has no access to it. And the map keys are repo-relative paths (e.g. `agent/runtime.py`), but `_on_directory_loaded` only has `full_path` (absolute) — it doesn't know the project root to compute the rel path.

**Fix:** Store the status map and project path on `self` in `_show_tree` so `_on_directory_loaded` can look up child status:
1. In `_show_tree`, after fetching `status_map`, store it: `self._git_status_map = status_map`.
2. Initialize `self._git_status_map: dict[str, str] = {}` in `__init__`.
3. Clear it in `_clear_all_state`: `self._git_status_map = {}`.
4. In `_on_directory_loaded`, compute the child's rel path against `self._project_path` and look up status:
```python
            rel_path = os.path.relpath(full_path, self._project_path) if self._project_path else full_path
            raw_status = self._git_status_map.get(rel_path, "")
            child = FileTreeRow(
                ...
                git_status=raw_status,
                git_status_display=git_status_to_display(raw_status),
                ...
            )
```

**Verify:** After fix, expand a subdirectory in a project with modified files — the Status column must show badges for modified children. (Manual test, or grep that `_on_directory_loaded` references `_git_status_map`.)

**Pattern:** incomplete-wiring
**Tests:** Add a non-GTK test or integration assertion that `_on_directory_loaded` uses `self._git_status_map`.

---

## BUG P2-3 — `_on_search_changed` kicks user out of tree view (functional, severity: bug)

**File:** `ui/views/file_tree.py:1853-1857`

**Bug:** Phase 2 made the search entry visible in tree mode (`_show_tree` sets `_search_entry.set_visible(True)`), but `_on_search_changed` (line 1853) **only** routes to picker behavior:
```python
    def _on_search_changed(self, entry):
        query = entry.get_text()
        if self._project_list_handler:
            self._project_list_handler.search(query)
            self._show_project_picker()   # ← rebuilds picker, exits tree!
```
If a user types in the search box while in tree mode, this calls `_show_project_picker()` — **destroying the tree view and rebuilding the project picker cards**. The user is kicked out of the file tree.

The spec (BUG #3, §3.4.5) explicitly requires a dispatcher:
```python
    def _on_search_changed(self, entry):
        if self._project_path is not None:
            # Tree mode — Phase 3 will wire the debounced filter here.
            # For Phase 2, do nothing (no-op) until Phase 3 adds the filter model.
            return
        # Picker mode — existing behavior
        query = entry.get_text()
        if self._project_list_handler:
            self._project_list_handler.search(query)
            self._show_project_picker()
```

**Fix:** Add the `if self._project_path is not None: return` guard at the top of `_on_search_changed`. Phase 3 will replace the `return` with the real debounced filter call.

**Verify:** After fix, typing in search while in tree mode must NOT call `_show_project_picker`. Grep that the guard exists.

**Pattern:** wrong-behavior-in-mode
**Tests:** Add an assertion that `_on_search_changed` does not call `_show_project_picker` when `_project_path` is set.

---

## Summary

| # | Severity | Pattern | File:line | Fix |
|---|----------|---------|-----------|-----|
| P2-1 | bug | missing-import | file_tree.py:870 (use), imports (fix) | Add `import os` |
| P2-2 | bug | incomplete-wiring | file_tree.py:1733 | Store `_git_status_map` on self, use in child loop |
| P2-3 | bug | wrong-behavior-in-mode | file_tree.py:1853 | Add `_project_path` guard to dispatcher |

All 3 are functional bugs that the test suite couldn't catch because GTK widget tests segfault in this headless sandbox. The core lesson: **add non-GTK tests for the integration points** (import presence, attribute existence, method-branch selection) that the GTK tests would otherwise cover.
