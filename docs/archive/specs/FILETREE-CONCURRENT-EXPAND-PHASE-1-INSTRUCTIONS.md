# PHASE 1 — Per-Parent Request Tokens (Code Fix)

**Spec:** `docs/specs/SPEC-FILETREE-CONCURRENT-EXPAND-FIX.md` (read in full first)
**Scope:** `ui/views/file_tree.py` ONLY. No other file.

## Objective

Replace the global `_current_request_id` counter with per-parent load tokens so
concurrent directory expansions no longer discard each other's children.

## Read Before You Touch

1. `prompts/steelFramedCodeWriter.md` — in full
2. `docs/specs/SPEC-FILETREE-CONCURRENT-EXPAND-FIX.md` — in full
3. `ui/views/file_tree.py` — the five edit sites: `__init__` state block
   (~line 453), `_clear_all_state` (~line 603), `_expand_directory`
   (~line 1978), `_on_directory_loaded` (~line 2031), `_collapse_directory`
   (~line 2087). Read each function in full before editing.

## Edits (5 total)

### Edit 1 — `FileTree.__init__` (~line 453)

Replace:
```python
        self._current_request_id = 0  # For async guard (BUG #7)
```
with:
```python
        # Per-parent async load tokens: parent full_path -> latest request id.
        # Only the newest load for a given directory may insert children.
        self._dir_load_requests: dict[str, int] = {}
```

### Edit 2 — `_clear_all_state` (~line 603)

Replace:
```python
        # Invalidate any in-flight async requests
        self._current_request_id += 1
```
with:
```python
        # Invalidate any in-flight async requests (per-parent tokens)
        self._dir_load_requests.clear()
```

### Edit 3 — `_expand_directory` (~line 1978)

Replace:
```python
        # BUG #7: Increment request ID, capture in closure
        self._current_request_id += 1
        request_id = self._current_request_id

        # Mark as expanded immediately for UI feedback
        row.props.expanded = True
        parent_path = row.props.full_path
        parent_depth = row.props.depth
```
with:
```python
        # Per-parent request token (replaces global BUG #7 counter): only the
        # newest load for THIS directory may insert children. Other
        # directories' in-flight loads are unaffected.
        parent_path = row.props.full_path
        self._dir_load_requests[parent_path] = self._dir_load_requests.get(parent_path, 0) + 1
        request_id = self._dir_load_requests[parent_path]

        # Mark as expanded immediately for UI feedback
        row.props.expanded = True
        parent_depth = row.props.depth
```

### Edit 4 — `_on_directory_loaded` (~line 2031)

Replace:
```python
        # BUG #7: Ignore stale callbacks
        if request_id != self._current_request_id:
            return
```
with:
```python
        # Per-parent staleness guard: discard unless this is the newest load
        # for THIS directory (superseded by re-expand, or invalidated by
        # _clear_all_state).
        if self._dir_load_requests.get(parent_row_obj.props.full_path) != request_id:
            return
```

### Edit 5 — `_collapse_directory` (~line 2087)

Replace:
```python
        parent_depth = row.props.depth
        row.props.expanded = False

        # BUG #7: Increment request ID to invalidate any in-flight async loads
        self._current_request_id += 1
```
with:
```python
        parent_depth = row.props.depth
        row.props.expanded = False
```
(No token mutation on collapse. Rationale in spec §3.4: the `expanded` check in
`_on_directory_loaded` discards loads for collapsed parents; expand is the only
operation that mints tokens, so a collapse→re-expand cycle naturally bumps to a
fresh token and discards the stale load — preventing duplicate children.)

## Verification (run all, paste full output)

1. `grep -c "_current_request_id" ui/views/file_tree.py` → must be **0**
2. `python3 -m pytest tests/test_file_tree_columnview.py -v 2>&1 | tail -20`
   under `xvfb-run -a` (GTK needs a display; known sandbox segfault on
   `TestFileTreeRowWidget::test_widget_creation` is environmental — note it, don't chase it)
3. `python3 -c "import ui.views.file_tree"` — import check
4. Full-suite quick check: `xvfb-run -a python3 -m pytest tests/ -x -q --ignore=tests/test_file_tree_columnview.py 2>&1 | tail -5` — confirm no new failures vs. baseline (pre-existing failures documented in `.crabcakes/coder-bugs.md` / prior post-mortems)

## Report Format

Report back with:
- Files changed (exact paths + line numbers after your edits)
- All four verification outputs pasted in full
- COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Edit 1: __init__ state — evidence: <grep line>
- [x/not done] Edit 2: _clear_all_state — evidence: <grep line>
- [x/not done] Edit 3: _expand_directory — evidence: <grep line>
- [x/not done] Edit 4: _on_directory_loaded — evidence: <grep line>
- [x/not done] Edit 5: _collapse_directory — evidence: <grep line>
- [x/not done] grep proof: _current_request_id count == 0 — evidence: <paste>
- [x/not done] pytest no new failures — evidence: <paste>
```
- Any related issues found but NOT fixed (flag, don't fix)
