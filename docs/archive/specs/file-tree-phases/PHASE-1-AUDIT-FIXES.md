# Phase 1 — Audit Fixes (5 bugs)

**Spec of record:** `docs/specs/SPEC-FILE-TREE-ENHANCEMENTS.md`
**Source:** Debugger adversarial audit + Supervisor verification (5 bugs: A-1, A-2, #1, #2, #3).

These are small, targeted fixes. One functional bug (#1), three test-collection/integrity bugs (A-1, #2, A-2), one wiring fix (#3).

**Critical lesson:** Before adding ANY new test class or method, grep for BOTH class-name AND method-name collisions. The audit found the same `name-collision` pattern twice — once at class level (A-1), once at method level (#2).

---

## Fix 1 — BUG #1: format_mtime future-timestamp nonsense (functional)

**File:** `ui/views/file_tree.py` — `format_mtime` function (~line 49).

**Bug:** Future timestamps (clock skew, NTP drift, manually-set mtimes) produce negative strings like "-1d ago", "-355540d ago". The `<= 0` guard doesn't catch positive future values.

**Fix:** After computing `diff = now - dt`, add a guard for negative days BEFORE the existing `if diff.days == 0:` branch:

```python
    diff = now - dt
    if diff.days < 0:  # future timestamp — show absolute date
        return dt.strftime("%b %d, %Y")
    if diff.days == 0:
        ...
```

**Test:** Add `tests/test_file_tree_helpers.py` (NEW file — no GTK imports needed, these are pure functions). Cover:
- `format_mtime(tomorrow)` → does NOT contain "ago", contains a month name
- `format_mtime(year_3000)` → contains "3000"
- `format_mtime(0)` → "—"
- `format_mtime(-1)` → "—"
- `format_size` regression tests: `format_size(0)` → "—", `format_size(1500)` → "1.5 KB", `format_size(1048576)` → "1.0 MB", `format_size(1024)` → "1.0 KB"
- `git_status_to_display` tests: `"M "` → "M", `" M"` → "M", `"??"` → "?", `" D"` → "D", `""` → "", `"!!"` → "!"

## Fix 2 — BUG A-1: Rename new TestStatusPorcelain class (test collection loss)

**File:** `tests/test_git_ops.py:639`

**Bug:** Two classes named `TestStatusPorcelain` (line 263 pre-existing tests `status()`, line 639 new tests `status_porcelain()`). The first is silently shadowed → `test_status_new_file` dropped from collection.

**Fix:** Rename ONLY the new class (line 639) from `TestStatusPorcelain` to `TestStatusPorcelainFn`. Do NOT touch the pre-existing class at line 263.

**Verify:** `python3 -m pytest tests/test_git_ops.py --co -q | grep -c test_status_new_file` must return 1.

## Fix 3 — BUG #2: Rename new test_count_clamping method (test collection loss)

**File:** `tests/test_git_ops.py:607`

**Bug:** Two methods named `test_count_clamping` inside `class TestFileLog` (line 512 pre-existing, line 607 new). The first is shadowed.

**Fix:** Rename ONLY the new method (line 607) from `test_count_clamping` to `test_count_clamping_with_line_counts`. Do NOT touch the pre-existing one at line 512.

**Verify:** `python3 -m pytest tests/test_git_ops.py --co -q | grep -c "Function test_count_clamping"` must return 2 (the original + the renamed one).

## Fix 4 — BUG A-2: Fix misleading test assertions

**File:** `tests/test_git_ops.py`

### Fix 4a — test_too_short_line_skipped (line ~724)

**Bug:** Calls `status_porcelain` on a clean repo (output ""). The `if len(line) < 4: continue` branch is never hit. The test passes but doesn't test what it claims.

**Fix:** Monkeypatch `gitpython.Repo` so `repo.git.status` returns a crafted string containing a too-short line. Assert the too-short line's path is NOT in the result. Example approach:

```python
def test_too_short_line_skipped(self, temp_repo, monkeypatch):
    """A porcelain line shorter than 4 chars is skipped (BUG #4)."""
    import git as gitpython
    repo = gitpython.Repo(temp_repo)

    # Force status to return a too-short malformed line
    class FakeGit:
        def status(self, *args, **kwargs):
            return "XY\n"  # 2 chars — no space separator, no path
    monkeypatch.setattr(repo, "git", FakeGit())

    result = status_porcelain(temp_repo)
    assert result == {}  # too-short line skipped, nothing parsed
```

### Fix 4b — test_worktree_rename_both_status_positions (line ~736)

**Bug:** Stages both the rename and the removal via `repo.index.add/remove`, producing an **index-rename** (`R `), NOT a worktree-rename (` R`). The `status_code[1] in ('R','C')` branch is untested.

**Fix:** Produce a TRUE worktree rename — rename the file in the working tree WITHOUT staging the rename, so git detects it as deleted(old) + untracked(new). Then the porcelain output will be ` D original.txt\n?? renamed.txt`. Assert that worktree deletions and untracked files are both parsed correctly (this exercises the worktree-column branch differently than an index rename).

Actually — a true worktree rename produces ` D old` + `?? new`, not ` R old -> new`. The ` R` worktree-rename format only appears when git detects a rename across the worktree/index boundary, which requires the file to have been staged under the old name and then renamed in the working tree. To get a real ` R` you would: stage `original.txt`, commit, then `os.rename` in the working tree (no `git add`). Git shows this as ` D original.txt` + `?? renamed.txt` — NOT a rename in porcelain output (renames are only detected in the index column `R ` by default, or with `git status --find-renames`).

**Revised fix:** Since worktree-column rename (` R`) is essentially never produced by default `git status --porcelain` (git only auto-detects renames in the index column), this test cannot be realistically constructed. Instead, test the parser's handling of a SYNTHETIC ` R` line directly via monkeypatch:

```python
def test_worktree_rename_both_status_positions(self, temp_repo, monkeypatch):
    """The parser checks BOTH status columns for R/C (BUG #25).

    Worktree-column rename (' R old -> new') is synthetic — git's default
    porcelain output only emits index-column renames ('R '). But the parser
    must handle both. We inject a synthetic ' R' line via monkeypatch.
    """
    import git as gitpython
    repo = gitpython.Repo(temp_repo)

    class FakeGit:
        def status(self, *args, **kwargs):
            return " R old_name.txt -> new_name.txt\n"
    monkeypatch.setattr(repo, "git", FakeGit())

    result = status_porcelain(temp_repo)
    # Worktree-column rename: destination path is the key
    assert "new_name.txt" in result, f"worktree rename key wrong: {result}"
    assert result["new_name.txt"] == " R"
```

## Fix 5 — BUG #3: Populate parent_full_path on drawer rows

**File:** `ui/views/file_tree.py:956` — the `drawer_row = FileTreeRow(...)` construction in `_toggle_drawer`.

**Bug:** The `parent_full_path` property was added (Phase 1) but never populated. It's dead code. Phase 2/3's `_filter_func` needs it to filter drawer rows with their parent file (spec BUG #26).

**Fix:** Add `parent_full_path=file_path` to the `FileTreeRow(...)` call at line 956:

```python
            drawer_row = FileTreeRow(
                display_name="",
                full_path="",
                is_dir=False,
                is_drawer=True,
                depth=file_row.props.depth,
                drawer_widget=revealer,
                is_open=True,
                parent_full_path=file_path,   # BUG #26 / BUG #3
            )
```

Do NOT add it to the other 3 `FileTreeRow(...)` sites (lines 735, 1546, 1597) — those are non-drawer rows.

---

## Verification (run ALL)

```bash
cd /path/to/projects/crabcakes

# 1. BUG #1 fix — no more negative strings
python3 -c "
from ui.views.file_tree import format_mtime
import time
t = int((time.time() + 86400) * 1_000_000_000)
r = format_mtime(t)
assert 'ago' not in r, f'future still shows ago: {r}'
print('tomorrow:', r)
print('year3000:', format_mtime(32503680000 * 10**9))
"

# 2. New helper tests pass
python3 -m pytest tests/test_file_tree_helpers.py -q

# 3. No more shadowed tests — full collection count
python3 -m pytest tests/test_git_ops.py --co -q 2>&1 | tail -1
# Should report MORE items than before (was 51, should now be 52+ after
# restoring test_status_new_file AND keeping the new tests)

# 4. test_status_new_file collected again
python3 -m pytest tests/test_git_ops.py --co -q | grep -c test_status_new_file
# Must be 1

# 5. Both test_count_clamping variants collected
python3 -m pytest tests/test_git_ops.py --co -q | grep -c "Function test_count_clamping"
# Must be 2 (original + renamed with_line_counts)

# 6. parent_full_path wired
grep -n "parent_full_path=file_path" ui/views/file_tree.py
# Must show 1 match at the drawer_row construction

# 7. Full target suite still green
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 7 verification commands
3. COMPLETENESS checklist (Fixes 1–5)
