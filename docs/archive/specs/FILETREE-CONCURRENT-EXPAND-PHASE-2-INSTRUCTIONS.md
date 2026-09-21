# PHASE 2 — Regression Tests + Stale Test Updates

**Spec:** `docs/specs/SPEC-FILETREE-CONCURRENT-EXPAND-FIX.md` §4 + §6 (read in full first)
**Scope:** `tests/test_file_tree_columnview.py` and `tests/test_file_tree_sort_filter.py` ONLY.

## Objective

1. Update the two tests that reference the removed `_current_request_id`.
2. Add the spec's regression tests, including the one that **fails on pre-fix code**.

## Read Before You Touch

1. `prompts/steelFramedCodeWriter.md` — in full
2. `docs/specs/SPEC-FILETREE-CONCURRENT-EXPAND-FIX.md` — in full
3. Both test files in full
4. `ui/views/file_tree.py` — `_expand_directory`, `_on_directory_loaded`,
   `_collapse_directory`, `_clear_all_state` (post-Phase-1 state)

## Part A — Update stale tests (2 edits)

### Edit A1 — `tests/test_file_tree_columnview.py` `TestFileTree::test_clear_all_state` (~line 259)

The test reads `tree._current_request_id` and asserts `== old_request_id + 1`.
Rewrite the assertion block to verify the per-parent tokens instead:
```python
        # Per-parent tokens (Phase 1 of FILETREE-CONCURRENT-EXPAND):
        # _clear_all_state must invalidate in-flight loads by clearing tokens.
        tree._dir_load_requests["/tmp/fake"] = 7
        tree._clear_all_state()
        assert tree._dir_load_requests == {}
```
(Adapt to the test's existing structure/fixtures; keep the rest of the test's
assertions — store emptied, drawers cleared — intact.)

### Edit A2 — `tests/test_file_tree_sort_filter.py` `TestStaleRequestGuard::test_stale_request_does_not_insert_children` (~line 271)

The test asserts the literal `"self._current_request_id"` appears in
`inspect.getsource(FileTree._on_directory_loaded)`. Update it to assert the new
guard AND keep the behavioral intent (stale load does not insert children):
- source assertion: `"self._dir_load_requests.get(parent_row_obj.props.full_path) != request_id"` in source
- then rewrite the behavioral part as a real scenario (mirror the new
  `test_concurrent_expand_all_dirs_receive_children` mechanics below, but
  delivering a stale token).

## Part B — New regression tests

Add to `tests/test_file_tree_columnview.py` a new class `TestConcurrentExpand`
using a **deterministic** harness — patch `GLib.idle_add` to queue callbacks in
a list (do NOT execute), patch `scan_directory` with a controllable fake, then
invoke queued callbacks manually. Pattern:

```python
@pytest.fixture
def tree_harness():
    tree = FileTree()
    queued = []
    def fake_idle_add(fn, *a):
        queued.append((fn, a)); return 0
    with patch("ui.views.file_tree.GLib.idle_add", side_effect=fake_idle_add), \
         patch("ui.views.file_tree.scan_directory") as fake_scan, \
         patch("ui.views.file_tree.threading.Thread"):
        yield tree, queued, fake_scan
```
(Check the module's actual `threading.Thread` usage — `_expand_directory`
starts a daemon thread; either patch `Thread` so `_do` runs inline in order,
or extract its behavior differently — whatever is deterministic. Do not sleep
or rely on timing.)

Required tests (spec §4 criteria 1–3):

1. `test_concurrent_expand_all_dirs_receive_children` — build a store with two
   dir rows; call `_on_expander_clicked`-equivalent expand on both; deliver
   both scans; assert BOTH dirs have children. **Must fail on pre-fix code** —
   verify by `git stash` before finalizing and note the result.
2. `test_collapse_reexpand_no_duplicate_children` — expand A, collapse A,
   re-expand A, deliver both loads (first with stale token mechanics); assert
   exactly one set of children.
3. `test_clear_state_discards_inflight_load` — expand A, `_clear_all_state()`,
   deliver load; assert no child rows inserted.

## Verification (run all, paste full output)

1. `xvfb-run -a python3 -m pytest tests/test_file_tree_columnview.py tests/test_file_tree_sort_filter.py -v 2>&1 | tail -15`
2. Baseline proof (the test must FAIL on pre-fix code — this is the bug's regression
   proof). Do NOT `git stash` and do NOT `git checkout --` a tracked file (review-layer
   auto-accept hazard, see `.crabcakes/context.md` 2026-07-19). Use the git-show redirect:
   ```
   cp ui/views/file_tree.py /tmp/ft_fixed.py                  # save fixed production file
   git show HEAD:ui/views/file_tree.py > ui/views/file_tree.py  # baseline production code
   xvfb-run -a python3 -m pytest tests/test_file_tree_columnview.py::TestConcurrentExpand -q 2>&1 | tail -5   # expect FAIL
   cp /tmp/ft_fixed.py ui/views/file_tree.py                  # restore the fix
   xvfb-run -a python3 -m pytest tests/test_file_tree_columnview.py::TestConcurrentExpand -q 2>&1 | tail -3   # expect PASS
   ```
   Paste both outputs (baseline FAIL, fixed PASS). Confirm `grep -c "_current_request_id" ui/views/file_tree.py` is still 0 after restoring.
3. `grep -rn "_current_request_id" tests/ | grep -v diff_viewer` → must be **0** lines (except any DiffViewer references, which belong to another class)

## Report Format

Files changed, all verification outputs pasted, COMPLETENESS checklist:

```
COMPLETENESS:
- [x/not done] Edit A1: test_clear_all_state updated — evidence: <paste>
- [x/not done] Edit A2: test_stale_request updated — evidence: <paste>
- [x/not done] Test 1: concurrent expand — evidence: <pass line> + <fails-on-baseline proof>
- [x/not done] Test 2: collapse/re-expand no dupes — evidence: <pass line>
- [x/not done] Test 3: clear-state discards — evidence: <pass line>
- [x/not done] grep proof — evidence: <paste>
```

Scope guard: tests only. No production code changes. Flag, don't fix.
