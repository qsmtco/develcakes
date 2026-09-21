# ARCH-GUARDS-1 — Instructions (Coder)

**Goal:** drive the last 2 failing tests to zero. Both are REAL layering issues,
not test rot. Supervisor-verified anchors (tree at 17b1172).

## EDIT A — `ui/views/left_panel.py:15` runtime view→handler import

Current (violation):
```python
from ui.handlers.file_tree_handler import FileTreeHandler
from ui.views.file_tree import FileTree
```
`test_views_do_not_import_handlers` fails on this (runtime import from
ui/handlers inside ui/views).

**Fix (inject, don't import):**
1. Delete the `from ui.handlers.file_tree_handler import FileTreeHandler` line.
2. `LeftPanel.__init__` gains an optional collaborator param (put it AFTER the
   existing params so positional callers are unaffected):
   `file_tree_handler=None`.
3. Store it (`self._file_tree_handler = file_tree_handler`) and use it wherever
   the class currently constructs/uses `FileTreeHandler`. READ the whole
   `__init__` + every use site of `FileTreeHandler` in the file first and
   replace ALL of them — a partial fix leaves an AttributeError at runtime.
4. If any use site *constructs* the handler (rather than just calling into it),
   it must now be tolerant of `None`: skip that wiring and log a debug line
   rather than crashing (the view must remain constructible standalone — the
   tests build it directly). Flag it in your report if this applies.
5. Keep `from ui.views.file_tree import FileTree` (same layer — legal).
6. **Wiring:** `ui/window.py` must pass the handler. Find where `LeftPanel(...)`
   is constructed, and where `FileTreeHandler` is constructed in window.py, and
   thread the existing instance through. If `FileTreeHandler` is constructed
   *inside* LeftPanel today, move that construction to window.py and inject it —
   and report the ordering carefully (window.py must build the handler before
   the panel).

**Verification for Edit A:** `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m
pytest tests/test_architecture.py -q` → the views guard goes green. Also run
the window/file-tree suites (test_file_tree_columnview, test_window_* if any,
test_main_content_tab_switch) to prove no wiring regression. The app must still
construct — `python3 -c "import ui.window"` smoke, plus any existing
window-construction test.

## EDIT B — document the GTK carve-out

`test_utils_gtk_imports_are_documented` fails because `utils/gtk_containers.py`
imports `gi.repository` but is not in the register.

**FIRST verify it is a legitimate carve-out** (do NOT document a layering break):
- `grep -n "^from\|^import" utils/gtk_containers.py` — confirm it does NOT
  import from `ui/`, `ui.handlers`, `gateway/`, or `agent/`. It is a GTK
  helper by design (widget container utilities), same class as `icons.py`.
- If it DOES import from a forbidden layer, STOP and flag instead — that is a
  real violation needing a different fix.

**If legitimate:**
1. Add `"gtk_containers.py"` to `documented_carve_outs` in
   `tests/test_architecture.py` (keep alphabetical).
2. Add it to the ARCHITECTURE.md §2 carve-out table/list (find it with
   `grep -n "gtk_safe_link.py" docs/ARCHITECTURE.md`; match the existing
   format exactly — same-commit rule, ARCHITECTURE.md §0).

## GATES

- Both architecture tests GREEN: `pytest tests/test_architecture.py -q` → 6 passed.
- No regression: run test_file_tree_columnview, test_main_content_tab_switch,
  test_window_* (if present), plus a `import ui.window` smoke.
- pyflakes /tmp/pf-venv3: 0 undefined on touched files.
- Full suite → **0F** (baseline is 2F/3844P + your mcp unit's delta). Report the
  exact count; this is the first 0F in the project's current baseline.
- One commit: `fix(arch): inject FileTreeHandler into LeftPanel + document gtk_containers carve-out (0F)`
- Flag (don't fix) anything adjacent.

Then STOP.
