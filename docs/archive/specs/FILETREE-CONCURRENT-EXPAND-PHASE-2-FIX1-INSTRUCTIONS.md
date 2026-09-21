# PHASE 2 FIX-1 — Vacuous Test Repairs (Debugger BUG #1–#3)

**Spec:** `docs/specs/SPEC-FILETREE-CONCURRENT-EXPAND-FIX.md` §4
**Source:** Debugger Phase-2 audit (3 bugs: 1 HIGH, 1 MEDIUM, 1 LOW)
**Scope:** `tests/test_file_tree_columnview.py` and `tests/test_file_tree_sort_filter.py` ONLY.

## Problem

Two of the new tests pass on **pre-fix** code (vacuous as regression guards), and
one relies on a brittle source-string assertion. Verified by Debugger via
`git stash push -- ui/views/file_tree.py`:

- `test_collapse_reexpand_no_duplicate_children` → PASSES pre-fix (HIGH)
- `test_clear_state_discards_inflight_load` → PASSES pre-fix (MEDIUM)
- `TestStaleRequestGuard` → only its source-string check fails pre-fix; its
  behavioral half is vacuous (LOW)

Root cause: the pre-fix global counter bumped on collapse/clear too, so those
single-dir scenarios are behaviorally identical pre/post-fix. The
**distinguishing property** of the new design is: *other directories' in-flight
loads survive unrelated expands/collapses/clears*.

## Fix 1 (BUG #1, HIGH) — new discriminating test

Add to `TestConcurrentExpand` in `tests/test_file_tree_columnview.py`:

```python
def test_collapse_reexpand_does_not_invalidate_other_dirs(self, tree_harness):
    """BUG #1 fix: per-parent tokens must not invalidate OTHER dirs' loads.

    Scenario: expand A, expand B, collapse A, re-expand A — all before any
    scan returns. Pre-fix global counter invalidated B's load (B empty);
    post-fix B's token is independent (B gets children). MUST fail pre-fix.
    """
```
Use the existing harness helpers (`_expand_row` / `_collapse_row` / deliver /
`_children`) per Debugger's sketch:
- rows A (`/proj/A`) and B (`/proj/B`), fake_scan returns one child per path
- expand A, expand B, collapse A, re-expand A, then deliver ALL queued loads
- assert A has its child AND B has its child (B assertion fails on pre-fix)

## Fix 2 (BUG #2, MEDIUM) — harden clear-state test

In `test_clear_state_discards_inflight_load`, after `_clear_all_state()` and
BEFORE delivering, add:
```python
        assert tree._dir_load_requests == {}  # tokens invalidated on clear
```
Add one docstring line: "Note: behavioral outcome matches pre-fix code (global
counter also invalidated on clear); the dict assertion pins the new mechanism."

## Fix 3 (BUG #3, LOW) — replace brittle source-string check

In `tests/test_file_tree_sort_filter.py` `TestStaleRequestGuard`:
- REMOVE the `assert "self._dir_load_requests.get(...) != request_id" in source`
  verbatim-substring check (breaks on benign refactors).
- KEEP/extend the behavioral scenario, and make it discriminating the same way
  as Fix 1: deliver a stale load for dir A while a fresh load for dir B is also
  in flight — assert A inserts nothing stale-derived, B still receives children.
- If `inspect.getsource` is then unused in the test, remove the import usage too.

## Verification (paste all)

1. `xvfb-run -a python3 -m pytest tests/test_file_tree_columnview.py tests/test_file_tree_sort_filter.py -v 2>&1 | grep -E "TestConcurrentExpand|TestStaleRequestGuard|test_clear_all_state"` — all PASS post-fix
2. Baseline proof for BOTH new discriminating tests (git-show redirect technique — NOT stash on tracked files is fine here since ONLY production file is reverted; but prefer the redirect as before):
   ```
   cp ui/views/file_tree.py /tmp/ft_fixed.py
   git show HEAD:ui/views/file_tree.py > ui/views/file_tree.py
   xvfb-run -a python3 -m pytest tests/test_file_tree_columnview.py::TestConcurrentExpand::test_collapse_reexpand_does_not_invalidate_other_dirs "tests/test_file_tree_sort_filter.py::TestStaleRequestGuard" -q 2>&1 | tail -3   # expect FAIL(S)
   cp /tmp/ft_fixed.py ui/views/file_tree.py
   xvfb-run -a python3 -m pytest tests/test_file_tree_columnview.py::TestConcurrentExpand "tests/test_file_tree_sort_filter.py::TestStaleRequestGuard" -q 2>&1 | tail -2   # expect all PASS
   ```
3. `grep -n "inspect.getsource" tests/test_file_tree_sort_filter.py` — confirm removed (or justified if retained for a non-brittle purpose)

## COMPLETENESS checklist required, plus baseline FAIL/PASS outputs.
