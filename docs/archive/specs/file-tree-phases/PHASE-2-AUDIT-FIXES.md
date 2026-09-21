# Phase 2 — Audit Fixes (3 functional bugs)

**Source:** Supervisor audit (`docs/specs/file-tree-phases/PHASE-2-SUPERVISOR-AUDIT.md`).
All 3 are functional runtime bugs hidden because GTK widget tests segfault in this sandbox.

---

## Fix 1 — BUG P2-1: Add missing `import os`

**File:** `ui/views/file_tree.py` imports block (after `import threading`).

**Bug:** Line 870 uses `os.path.relpath()` but `os` is never imported → `NameError` on project open.

**Fix:** Add `import os` to the imports.

**Verify:**
```bash
python3 -c "import ui.views.file_tree as ft; print('os' in dir(ft))"
# → True
```

## Fix 2 — BUG P2-2: Child rows don't get git status

**File:** `ui/views/file_tree.py` — `_on_directory_loaded` (line ~1733) + `__init__` + `_clear_all_state`.

**Bug:** `_on_directory_loaded` hardcodes `git_status=""` / `git_status_display=""` for all child rows. Only root files show status. Subdirectory files (the common case) show blank.

**Fix (3 steps):**

1. In `__init__`, add: `self._git_status_map: dict[str, str] = {}`
2. In `_clear_all_state`, add: `self._git_status_map = {}`
3. In `_show_tree`, after `status_map = self._on_get_git_status() or {}`, add: `self._git_status_map = status_map`
4. In `_on_directory_loaded`, in the child-loop, BEFORE constructing `FileTreeRow(...)`, compute the child's git status:
```python
            rel_path = os.path.relpath(full_path, self._project_path) if self._project_path else full_path
            raw_status = self._git_status_map.get(rel_path, "")
```
Then pass `git_status=raw_status` and `git_status_display=git_status_to_display(raw_status)` to the child `FileTreeRow(...)` (replacing the hardcoded `""`).

**Verify:**
```bash
grep -n "_git_status_map" ui/views/file_tree.py
# → 4 matches: __init__, _clear_all_state, _show_tree, _on_directory_loaded
```

## Fix 3 — BUG P2-3: Search kicks user out of tree view

**File:** `ui/views/file_tree.py:1853` — `_on_search_changed`.

**Bug:** Search is visible in tree mode, but `_on_search_changed` routes ALL search input to `_show_project_picker()`, destroying the tree view.

**Fix:** Add a guard at the top of `_on_search_changed`:
```python
    def _on_search_changed(self, entry):
        """Filter on search-changed. Routes to picker or tree handler."""
        if self._project_path is not None:
            # Tree mode — Phase 3 will wire the debounced filter here.
            # For Phase 2, no-op until Phase 3 adds the filter model.
            return
        # Picker mode — existing behavior
        query = entry.get_text()
        if self._project_list_handler:
            self._project_list_handler.search(query)
            self._show_project_picker()
```

**Verify:**
```bash
grep -n "if self._project_path is not None" ui/views/file_tree.py
# → 1 match in _on_search_changed
```

---

## Verification (run ALL)

```bash
cd /path/to/projects/crabcakes

# 1. os imported
python3 -c "import ui.views.file_tree as ft; print('os' in dir(ft))"
# → True

# 2. _git_status_map wired (4 references)
grep -c "_git_status_map" ui/views/file_tree.py
# → 4

# 3. search dispatcher guarded
grep -c "if self._project_path is not None" ui/views/file_tree.py
# → 1

# 4. full test suite still green
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py -q
# → 137 passed

# 5. child loop uses git status (not hardcoded "")
sed -n '/_on_directory_loaded/,/insert_pos/p' ui/views/file_tree.py | grep "git_status"
# → should show raw_status / git_status_to_display, NOT git_status=""
```

## Report back with

1. `git diff --stat`
2. Output of all 5 verification commands
3. COMPLETENESS checklist (Fixes 1–3)
