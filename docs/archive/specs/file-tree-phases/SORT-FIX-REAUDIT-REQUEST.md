# Sort-Fix Re-Audit Request

**Scope:** The 5 fixes from Debugger's audit on the SortListModel removal fix.
- BUG #1+#10: drawer parent_full_path now matches file's parent; drawer full_path = file path; _sort_store_in_place separates drawers, sorts non-drawers, re-inserts drawers after matching file
- BUG #2: selection saved by object identity before splice, restored after
- BUG #4: parent_row_obj captured in _expand_directory, _on_directory_loaded re-finds index via _find_row_index
- BUG #8: dead make_key removed
- BUG #9: duplicate @staticmethod removed

175 tests pass. Supervisor verified drawer adjacency + hierarchy preservation with direct GTK4 test.

Confirm the fixes hold. Check for regressions. Report in ## Audit Report format. Do NOT fix.
