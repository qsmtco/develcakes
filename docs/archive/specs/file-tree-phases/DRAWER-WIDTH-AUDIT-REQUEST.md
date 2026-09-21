# Drawer Width Fix — Audit Request

**Scope:** ui/views/file_tree.py — hide Status/Size/Modified columns when any drawer is open.

**Changes:**
- `_col_status`, `_col_size`, `_col_modified` stored on self in __init__ and _show_tree
- `_update_column_visibility_for_drawers()` helper: hides the 3 columns when `len(self._drawer_paths) > 0`
- Called at: _toggle_drawer open (1404), _toggle_drawer close (1371), _on_revealer_child_revealed (1459), _show_tree end (1171), _collapse_directory (2112)

**Supervisor verified:** 176 tests pass, all call sites present.

**Focus areas:**
- Are all `_drawer_paths` mutation sites covered? Any path that adds/removes from `_drawer_paths` without calling the visibility helper?
- Does `_clear_all_state` reset the columns? (It clears `_drawer_paths` but may not call the helper)
- Could `_update_column_visibility_for_drawers` be called when `_col_status` is None (e.g., in picker mode before _show_tree runs)?
- Does `set_visible(False)` on a ColumnViewColumn cause layout issues if the column view is mid-render?

Report in ## Audit Report format. Do NOT fix.
