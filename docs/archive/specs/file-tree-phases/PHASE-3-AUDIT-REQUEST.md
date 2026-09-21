# Phase 3 — Supervisor verification + Debugger audit request

## Supervisor verification status

All structural checks pass:
- 9 new methods present (`_init_sort_filter`, `_apply_sort`, `_apply_filter`, `_build_sorter`, `_filter_func`, `_on_sort_dropdown_changed`, `_on_search_changed_tree_cb`, `set_on_sort_changed`, `set_on_get_sort_mode`)
- GTK4 types available (SortListModel, FilterListModel, CustomSorter, CustomFilter)
- 138/138 tests pass
- M6 wiring: `_init_sort_filter` at end of `_show_tree`, `_apply_sort` at end of `_on_directory_loaded`
- BUG #9: search timeout cancelled in `_clear_all_state`
- `_clear_all_state` nulls `_sort_model`/`_filter_model`
- Dropdown hidden in picker, visible in tree
- `_on_search_changed` routes to tree debounce in tree mode, picker in picker mode

## Supervisor-found bug

### BUG P3-1 — Default sort never applied when no handler callback set (severity: bug)

**File:** `ui/views/file_tree.py` — `_show_tree` end (line ~1058-1067).

`_init_sort_filter()` creates the SortListModel with `sorter=None` (no sorting). The `_apply_sort` call is inside `if self._on_get_sort_mode:` — which is `None` in Phase 3 (handler not wired until Phase 4). So **rows appear in insertion order, not sorted**, until the user manually clicks the dropdown.

Even in Phase 4, if the handler callback is somehow None (degraded mode), the same issue occurs.

**Fix:** After `_init_sort_filter()`, always apply the default sort (name_asc), THEN optionally restore from handler:
```python
        self._init_sort_filter()
        # Always apply default sort first
        self._apply_sort("name_asc")
        # Then restore saved mode if handler provides one
        if self._on_get_sort_mode:
            ...
```

---

## Debugger — your audit

**Scope:** Phase 3 changes to `ui/views/file_tree.py` only (~204 insertions).

**Files:** `ui/views/file_tree.py` — sort/filter model chain, 6 comparators, sort dropdown, search debounce, `_filter_func`, `_clear_all_state` timeout cancellation, M6 wiring.

**My verification found 1 bug (P3-1 above).** Confirm or refute it, then run your full 11-section adversarial probe. Focus areas:

- **Comparator correctness:** Do all 6 comparators handle the directory-first invariant correctly? What about equal-modified-time ties (stable sort)?
- **`_filter_func` with drawer rows:** A drawer row's `parent_full_path` is set, but if the user searches for a term that matches the drawer's parent file, should the drawer row appear even if the file row itself is filtered out? (Current: drawer checks `parent_full_path`, but if the parent file row is filtered out, the drawer will show with no parent above it — orphaned drawer.)
- **`_on_search_changed_tree_cb` timeout:** The `_apply` closure captures `query` by reference. If the user types fast, multiple timeouts could stack. Verify the cancel-before-add pattern is correct.
- **`_clear_all_state` model nulling:** After nulling `_sort_model`/`_filter_model`, the selection model still points at the orphaned filter model. Is this a problem for picker mode? (Picker uses cards, not ColumnView — verify.)
- **Sort dropdown `notify::selected` signal:** Does it fire during programmatic `set_selected()` in the restore path? If so, it would call `_on_sort_dropdown_changed` which calls `_apply_sort` again — double application.
- **Comparator integer overflow:** `b.props.modified_time - a.props.modified_time` — both are Unix timestamps (~1.7e9). The difference is bounded, but verify GTK4's CustomSorter expects -1/0/1, not arbitrary integers. Does GTK4 normalize?

Report bugs in `## Audit Report` format with `**Pattern:**` tags. Do NOT fix.
