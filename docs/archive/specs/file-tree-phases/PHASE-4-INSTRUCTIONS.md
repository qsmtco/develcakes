# Phase 4 — Handler + Persistence + Wiring + ARCHITECTURE.md (FINAL PHASE)

**Spec of record:** `docs/specs/SPEC-FILE-TREE-ENHANCEMENTS.md` (§3.5, §3.7, §10)
**Prerequisite:** Phase 3 complete (sort/filter chain working, all audits passed).

This is the final phase. Creates the handler, wires it into LeftPanel, adds
per-project sort persistence, invalidates git status on project open/close,
and updates ARCHITECTURE.md.

---

## Task 1 — Create `ui/handlers/file_tree_handler.py` (NEW)

**No GTK imports.** Manages: sort preference persistence + git status cache.

```python
# ui/handlers/file_tree_handler.py
# File tree logic: git status caching, sort preference persistence.
# No GTK imports. Communicates with view via callbacks.

import os
import json

from utils.git_ops import status_porcelain


class FileTreeHandler:
    """Manages file tree logic: git status caching, sort preference persistence.

    No GTK imports. Communicates with view via callbacks set on the view instance.
    """

    # Valid sort modes (BUG #13 whitelist)
    _VALID_SORT_MODES = frozenset({
        "name_asc", "name_desc", "modified_asc", "modified_desc",
        "size_asc", "size_desc"
    })

    def __init__(self, project_path: str = ""):
        self._project_path = project_path
        self._git_status_cache: dict[str, str] = {}
        self._git_status_dirty = True
        self._sort_mode = "name_asc"
        self._prefs_path = ""
        if project_path:
            self._prefs_path = os.path.join(project_path, ".crabcakes", "file_tree_prefs.json")
            self._load_prefs()

    def refresh_git_status(self) -> dict[str, str]:
        """Run git status --porcelain, cache result, return parsed map.

        Returns empty dict if not a git repo or on any error.
        Cached until invalidate_git_status() is called.
        """
        if not self._git_status_dirty:
            return self._git_status_cache
        self._git_status_cache = status_porcelain(self._project_path)
        self._git_status_dirty = False
        return self._git_status_cache

    def invalidate_git_status(self) -> None:
        """Mark git status cache as dirty — next refresh will re-run git status."""
        self._git_status_dirty = True

    def get_sort_mode(self) -> str:
        return self._sort_mode

    def set_sort_mode(self, mode: str) -> None:
        """Set sort mode, save to persistence. Validates against whitelist (BUG #13)."""
        if mode not in self._VALID_SORT_MODES:
            return
        if mode != self._sort_mode:
            self._sort_mode = mode
            self._save_prefs()

    def set_project_path(self, path: str) -> None:
        """Called when project switches. Invalidates caches, loads prefs."""
        self._project_path = path
        self.invalidate_git_status()
        if path:
            self._prefs_path = os.path.join(path, ".crabcakes", "file_tree_prefs.json")
            self._load_prefs()
        else:
            self._prefs_path = ""
            self._sort_mode = "name_asc"

    def _load_prefs(self) -> None:
        """Load sort preference from disk. Validates mode against whitelist (BUG #13)."""
        if not self._prefs_path or not os.path.exists(self._prefs_path):
            self._sort_mode = "name_asc"
            return
        try:
            with open(self._prefs_path) as f:
                data = json.load(f)
                loaded = data.get("sort_mode", "name_asc")
                if loaded in self._VALID_SORT_MODES:
                    self._sort_mode = loaded
                else:
                    self._sort_mode = "name_asc"
        except Exception:
            self._sort_mode = "name_asc"

    def _save_prefs(self) -> None:
        """Save sort mode to per-project prefs file."""
        if not self._prefs_path:
            return
        os.makedirs(os.path.dirname(self._prefs_path), exist_ok=True)
        with open(self._prefs_path, "w") as f:
            json.dump({"sort_mode": self._sort_mode}, f)
```

**IMPORTANT:** Note the `_VALID_SORT_MODES` order matches the Phase 3 fix (BUG #5): `modified_asc` before `modified_desc`, `size_asc` before `size_desc`.

## Task 2 — Create `tests/test_file_tree_handler.py` (NEW)

Test the handler with NO GTK imports (pure Python). Cover:

- `__init__` with empty path → sort_mode is "name_asc", git_status_cache is {}
- `__init__` with a tmp_path that has `.crabcakes/file_tree_prefs.json` with valid mode → loads it
- `_load_prefs` with invalid mode in file → falls back to "name_asc" (BUG #13)
- `_load_prefs` with missing file → "name_asc"
- `_load_prefs` with corrupt JSON → "name_asc"
- `set_sort_mode` with valid mode → updates + saves to disk
- `set_sort_mode` with invalid mode → ignored (BUG #13)
- `set_sort_mode` with same mode → no disk write (idempotent)
- `refresh_git_status` on non-repo → {} 
- `refresh_git_status` caches (calls status_porcelain once, returns cache on 2nd call)
- `invalidate_git_status` marks dirty
- `set_project_path` with new path → invalidates git, loads prefs
- `set_project_path("")` → resets sort to "name_asc"
- `_save_prefs` creates `.crabcakes/` dir if missing

Use `tmp_path` for file-based tests. Use `unittest.mock.patch` on `utils.git_ops.status_porcelain` for git status tests (don't create real repos).

## Task 3 — Wire handler into LeftPanel

**File:** `ui/views/left_panel.py`

### 3a. Import + create handler in `__init__`:

```python
from ui.handlers.file_tree_handler import FileTreeHandler
```

In `__init__`, after the FileTree is created (line ~102), add:

```python
        # FileTreeHandler — manages sort prefs + git status cache (no GTK)
        self._file_tree_handler = FileTreeHandler()
        # Wire view callbacks to handler
        self._file_tree.set_on_sort_changed(self._file_tree_handler.set_sort_mode)
        self._file_tree.set_on_get_sort_mode(self._file_tree_handler.get_sort_mode)
        self._file_tree.set_on_get_git_status(self._file_tree_handler.refresh_git_status)
```

**NOTE:** The `set_on_get_git_status` wiring already exists in `window.py:424` (from Phase 2 BUG #1 fix). **Remove that line from window.py** — the handler now provides it via LeftPanel. If both are wired, the LeftPanel one wins (set last). Clean up the duplicate.

### 3b. Wire project open/close:

LeftPanel doesn't have direct `on_project_opened` / `on_project_closed` methods. The project lifecycle flows through `open_project_view` and `close_project_view`. Add handler calls:

In `open_project_view` (after `self._is_project_view_open = True`), add:
```python
        # The project name/path isn't directly available here — the FileTree
        # already has it via load_project(). Use the FileTree's project path.
        if self._file_tree._project_path:
            self._file_tree_handler.set_project_path(self._file_tree._project_path)
            self._file_tree_handler.invalidate_git_status()
```

In `close_project_view` (at the start, after the idempotency guard), add:
```python
        self._file_tree_handler.set_project_path("")
```

### 3c. Remove the duplicate wiring in window.py:

**File:** `ui/window.py:424`

Remove the line:
```python
        left_panel._file_tree.set_on_get_git_status(self._project_handler.get_git_status)
```

The handler now provides git status via `refresh_git_status()`, wired in Task 3a. Also remove the `get_git_status` method from `ProjectHandler` if it's no longer used elsewhere (grep first).

## Task 4 — Update ARCHITECTURE.md

**File:** `docs/ARCHITECTURE.md`

### 4a. Add `file_tree_handler.py` to the handler list (§3.16 or equivalent):

Find the handler list/table and add:
```
│   ├── file_tree_handler.py     # Sort prefs + git status cache (no GTK)
```

### 4b. Update the FileTree section (§3.8 or wherever file_tree.py is documented):

Add a subsection documenting:
- FileTreeRow: now 22 properties (12 original + 9 file metadata + parent_full_path)
- ColumnView + SortListModel + FilterListModel chain (in-place mutation pattern)
- Handler/view split: handler = prefs + git cache; view = widgets + sort/filter models
- Background thread safety (generation counter + path capture)
- Search debounce (150ms GLib.timeout_add) and timeout lifecycle
- 4-column layout: Name, Status (git badge), Size (human-readable), Modified (relative time)
- Sort: 6 modes, depth-aware comparator preserving tree hierarchy, drawer-adjacency invariant

### 4c. Update any handler count (if the doc says "21 handlers" — now 22):

Search for "21" in handler context and update to 22.

## What NOT to do

- Do NOT change the comparator logic (Phase 3 finalized it)
- Do NOT change the factory classes (Phase 2 finalized them)
- Do NOT modify `scan_directory` or `GitResult` or `status_porcelain`
- Do NOT add new GObject properties

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Handler imports cleanly (no GTK)
python3 -c "
import sys
# Verify no GTK imports in the handler
with open('ui/handlers/file_tree_handler.py') as f: src = f.read()
assert 'gi.require_version' not in src, 'GTK imported in handler!'
assert 'from gi.repository' not in src, 'GTK imported in handler!'
from ui.handlers.file_tree_handler import FileTreeHandler
h = FileTreeHandler()
assert h.get_sort_mode() == 'name_asc'
print('handler OK — no GTK imports')
"

# 2. Handler tests pass
python3 -m pytest tests/test_file_tree_handler.py -q

# 3. LeftPanel wiring present
grep -n "FileTreeHandler\|set_on_sort_changed\|set_on_get_sort_mode" ui/views/left_panel.py

# 4. window.py duplicate removed
grep -c "set_on_get_git_status" ui/window.py  # should be 0

# 5. ARCHITECTURE.md updated
grep -c "file_tree_handler" docs/ARCHITECTURE.md  # should be >= 2

# 6. Full suite
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py tests/test_file_tree_handler.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 6 verification commands
3. COMPLETENESS checklist (Tasks 1–4)
