# Phase 3 — Re-audit request (fixes for 7 bugs)

**Scope:** Phase 3 audit fixes applied to `ui/views/file_tree.py` + new `tests/test_file_tree_sort_filter.py` (22 tests).

**Fixes applied (7 bugs):**
1. BUG #1 (CRITICAL): All 6 comparators now have 3-arg signature `(a, b, _ud=None)` — empirically verified sort works via real SortListModel
2. BUG #2 (CRITICAL): `_filter_func` now takes `(item, query)` — empirically verified filter works via real FilterListModel
3. P3-1 + BUG #3: Default sort applied unconditionally; signal blocked during programmatic restore
4. BUG #4: Drawer rows return 0 from comparators (stay at insertion position)
5. BUG #6: Dead `_sort_changed_count` removed
6. BUG #5: None guard returns False
7. BUG #7: Dropdown reset to 0 with signal blocked at start of `_show_tree`

**Supervisor verification:** All 7 confirmed. 160/160 tests pass. Real SortListModel + FilterListModel probes confirm sorting and filtering work end-to-end. Drawer invariant verified (drawer stays after parent).

**Your job:** Re-audit the fixes. Confirm the 7 bugs are resolved. Check for new issues introduced by the fixes — especially:
- The `_build_sorter` is an instance method but is called in tests with a dummy `D()` — does it actually use `self`? If not, should it be a `@staticmethod`?
- The drawer-row `return 0` approach — does it work correctly when BOTH items are drawers? When a drawer is compared against a directory?
- The signal block/unblock pattern — are both sites (restore + reset) correct? Could the unblock be missed on an exception path?
- The 22 new tests — do they actually assert the RIGHT things, or are there misleading assertions like the Phase 1 BUG A-2 pattern?
- Is there a test for the sort-dropdown feedback loop (BUG #3)?

Report bugs in `## Audit Report` format. Do NOT fix.
