# Phase 1a Instructions: utils/git_ops.py — Add Two Functions

**Spec:** SPEC-ONE-CLICK-DIFF.md (§2.1)
**Phase:** 1 of 3 (git_ops → diff_card → diff_viewer → wiring)
**Target files:** 2 files max (git_ops.py + test_git_ops.py)

---

## Changes Required

### 1. Add `diff_file_against_working_tree()` — NEW FUNCTION

**Insert after:** `diff_file_against()` (after line 176 in current source)

```python
def diff_file_against_working_tree(project_path: str, sha: str, file_path: str) -> GitResult:
    """Diff for a single file between commit sha and working tree.

    Unlike diff_file_against() (which diffs sha→HEAD), this includes
    uncommitted changes. Equivalent to: git diff <sha> -- <file_path>

    Use during active review when agents have edited files but not committed.

    SHA validation: sha is validated via _VALID_SHA_RE before being passed
    to git, matching the MED-11 fix pattern in checkout_paths().
    """
    # Validate SHA — prevent git argument injection (MED-11 pattern)
    if sha != "HEAD" and not _VALID_SHA_RE.match(sha):
        return GitResult(success=False, stdout="", error=f"Invalid git ref: {sha}", sha=None)

    try:
        repo = gitpython.Repo(project_path)
        diff_text = repo.git.diff(sha, "--", file_path)
        return GitResult(success=True, stdout=diff_text, error="", sha=repo.head.commit.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)
```

**Key requirements:**
- SHA validation via `_VALID_SHA_RE` (line 44) BEFORE git call
- `repo.git.diff(sha, "--", file_path)` — sha → working tree (includes uncommitted)
- Returns `GitResult(success, stdout, error, sha=HEAD hexsha)`
- Follows exact pattern of `diff_file_against()` at line 168

---

### 2. Add `file_log()` — NEW FUNCTION

**Insert after:** `get_recent_commits()` (after line 222 in current source)

```python
def file_log(project_path: str, file_path: str, count: int = 20) -> GitResult:
    """Commit history for a single file.

    Returns: GitResult with stdout = lines of "SHA\\x1FISO_DATE\\x1FMESSAGE"
    Uses --follow to track renames. Caps at count entries.

    Format: fields separated by ASCII Unit Separator (\\x1f) to avoid
    collisions with pipe characters in commit messages.
    """
    # Clamp count to safe bounds (M13 fix)
    count = max(1, min(count, 100))

    try:
        repo = gitpython.Repo(project_path)
        log_text = repo.git.log(
            "--follow",
            "--format=%H%x1f%cI%x1f%s",
            f"-n {count}",
            "--", file_path,
        )
        return GitResult(success=True, stdout=log_text, error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)
```

**Key requirements:**
- Count clamped to `1 <= count <= 100`
- Format: `%H%x1f%cI%x1f%s` (ASCII Unit Separator `\x1f`, NOT pipe `|`)
- `--follow` tracks renames
- Returns `GitResult(success, stdout, error, sha=None)`

---

### 3. Write Unit Tests

**File:** `tests/test_git_ops.py`

Add tests for:
- `test_diff_file_against_working_tree` — diff against HEAD, diff against specific SHA, invalid SHA rejected
- `test_file_log` — history for tracked file, empty for untracked, count clamping, pipe in message

---

## Rules (steelFramedCodeWriter.md)

- Read `utils/git_ops.py` in full before editing
- Read `tests/test_git_ops.py` in full before editing
- Verify every claim with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `GitResult`, `_VALID_SHA_RE`, `_safe_error`, `gitpython.Repo`
- Hard part first: implement functions, then tests
- Wire it or delete it — no stubs

---

## Deliverable Expectations

Report back with:

```
Files changed:
- utils/git_ops.py:XX-YY (added diff_file_against_working_tree)
- utils/git_ops.py:AA-BB (added file_log)
- tests/test_git_ops.py:CC-DD (new tests)

Verification:
pytest tests/test_git_ops.py::test_diff_file_against_working_tree -v
→ [paste full output]
pytest tests/test_git_ops.py::test_file_log -v
→ [paste full output]
grep -n "diff_file_against_working_tree\|file_log" utils/git_ops.py
→ [paste output]

COMPLETENESS:
- [x/not done] Edit 1: diff_file_against_working_tree with SHA validation — evidence: pytest output
- [x/not done] Edit 2: file_log with \\x1f separator and count clamping — evidence: pytest output
- [x/not done] Edit 3: tests for both functions — evidence: pytest output
```

---

## Word Marker

**please write**