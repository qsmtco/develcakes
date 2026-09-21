# Phase T2-RL1 — git_ops.commit() empty-check fix

**Source:** `docs/post-mortems/2026-06-16-REVIEW-LAYER-INVESTIGATION.md` (Phase 3, Root cause + Patch 1)
**Severity:** MEDIUM (6 of 11 recent Accept commits are empty; 1 has wrong file; 1 has incomplete file list)
**Risk:** Low
**Lines:** +5 in `utils/git_ops.py`, +20 in tests

## Goal

`utils/git_ops.py:commit()` currently calls `repo.index.commit(message)` unconditionally. If the working tree is clean (no staged changes), this creates an empty commit with the given message. Six of the 11 recent "Accept: Modified" commits in the git log are exactly this bug — captain's signature on nothing.

Fix: add an `allow_empty` parameter that defaults to `False`. When `allow_empty=False` and the working tree is clean, return a `GitResult(success=False, error="nothing to commit (working tree clean)")` instead of committing.

The checkpoint caller (line 164 of `review_handler.py`) is the ONE valid use of empty commits (checkpoint as a SHA marker). It will pass `allow_empty=True`. All other callers (accept, feed accept) use the default.

## Files to change

1. `utils/git_ops.py` — add the empty-check
2. `tests/test_git_ops.py` — add tests for the new behavior

## Edit 1: `utils/git_ops.py` — add allow_empty parameter

**Anchor:** the `commit` function (line 75-83). Find this pattern:

```python
def commit(project_path: str, message: str) -> GitResult:
    """Commit staged changes. Returns SHA in result.sha."""
    try:
        repo = gitpython.Repo(project_path)
        commit_obj = repo.index.commit(message)
        return GitResult(success=True, stdout=str(commit_obj.hexsha), error="", sha=commit_obj.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=str(e), sha=None)
```

**Replace with:**

```python
def commit(project_path: str, message: str, allow_empty: bool = False) -> GitResult:
    """Commit staged changes. Returns SHA in result.sha.

    Args:
        project_path: Path to the git repository.
        message: Commit message.
        allow_empty: If True, allow empty commits (no staged changes). Use only
            for checkpoint markers where the SHA itself is the desired output.
            Default is False — refuse to create empty commits, since they
            pollute the git log with the captain's signature on nothing.

    Returns:
        GitResult. If allow_empty is False and the working tree is clean,
        returns success=False with error="nothing to commit (working tree clean)".
    """
    try:
        repo = gitpython.Repo(project_path)
        # Empty-check: refuse to commit if there's nothing staged (unless caller
        # explicitly allows it via allow_empty=True).
        if not allow_empty and not repo.index.diff("HEAD"):
            return GitResult(
                success=False, stdout="", error="nothing to commit (working tree clean)",
                sha=None,
            )
        commit_obj = repo.index.commit(message)
        return GitResult(success=True, stdout=str(commit_obj.hexsha), error="", sha=commit_obj.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=str(e), sha=None)
```

The change is:
1. Add `allow_empty: bool = False` parameter
2. Add the empty-check before the commit
3. Update the docstring

## Edit 2: `tests/test_git_ops.py` — add tests for empty-check

**Anchor:** append new test methods to the existing test class in `tests/test_git_ops.py`. Look at the existing style and follow it.

Recommended tests (use the existing test patterns from the file):

```python
    def test_commit_refuses_empty_when_allow_empty_false(self):
        """When the working tree is clean and allow_empty=False (default),
        commit() returns success=False with 'nothing to commit' error.
        No commit is created.
        """
        # Use the existing test repo setup pattern (whatever the file uses)
        # ... 
        # Stage nothing (working tree clean)
        result = git_ops.commit(project_path, "test empty commit")
        assert result.success is False
        assert "nothing to commit" in result.error
        # Verify no commit was created (HEAD didn't change)
        # ...

    def test_commit_allows_empty_when_allow_empty_true(self):
        """When the working tree is clean and allow_empty=True, commit() creates
        an empty commit with the given message. Use only for checkpoint markers.
        """
        # ...
        result = git_ops.commit(project_path, "test checkpoint", allow_empty=True)
        assert result.success is True
        assert result.sha is not None
        # ...

    def test_commit_succeeds_when_changes_staged(self):
        """When the working tree has staged changes, commit() creates a
        non-empty commit with the given message. allow_empty has no effect.
        """
        # ...stage a file, then commit
        result = git_ops.commit(project_path, "real commit")
        assert result.success is True
        assert result.sha is not None
        # Verify the commit has the staged file in its diff
        # ...
```

Note: the exact test setup (creating a temp git repo, staging files) will depend on the existing test infrastructure in `tests/test_git_ops.py`. Read the file first to understand the patterns (fixtures, helpers) and follow them.

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do NOT modify any other function in `utils/git_ops.py` (e.g., `stage_all`).
- Do NOT modify the `GitResult` dataclass.
- Do NOT change the default behavior — `allow_empty=False` must be the default.
- Do NOT change the success/error reporting format. The empty-check returns `success=False` with a specific error message, matching the existing pattern.
- The fix must be backwards compatible. Existing callers that don't pass `allow_empty` should get the new defensive behavior (refuse empty commits). This is a behavior change for those callers — but the existing callers are bug-riddled (creating empty commits) and the new behavior is the correct one.

## Verification (run yourself, paste output in report)

1. The function has the new parameter:
   ```
   grep -n "allow_empty" utils/git_ops.py
   ```
   Expected: 3 matches (parameter declaration, docstring mention, if-check usage).

2. The new tests pass:
   ```
   python3 -m pytest tests/test_git_ops.py -v 2>&1 | tail -20
   ```
   Expected: at least 3 new tests pass (existing tests should also still pass — count increase).

3. End-to-end: a clean working tree + commit → no new commit created:
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess

   tmpdir = tempfile.mkdtemp()
   # Init a git repo
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 'test@test.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'Test'], check=True)
   # Make an initial commit so HEAD exists
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)

   # Get HEAD sha before
   head_before = subprocess.run(['git', '-C', tmpdir, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
   print(f'HEAD before: {head_before}')

   from utils import git_ops
   result = git_ops.commit(tmpdir, 'should refuse empty')
   print(f'commit result: success={result.success}, error={result.error!r}')

   head_after = subprocess.run(['git', '-C', tmpdir, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
   print(f'HEAD after:  {head_after}')
   assert result.success is False, f'commit should have refused, got success'
   assert 'nothing to commit' in result.error
   assert head_before == head_after, 'HEAD should not have changed'
   print('OK: empty commit refused, HEAD unchanged')
   "
   ```
   Expected: `OK: empty commit refused, HEAD unchanged`.

4. End-to-end: a clean working tree + commit with allow_empty=True → empty commit created:
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess
   tmpdir = tempfile.mkdtemp()
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 'test@test.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'Test'], check=True)
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)
   from utils import git_ops
   result = git_ops.commit(tmpdir, 'checkpoint marker', allow_empty=True)
   assert result.success is True
   assert result.sha is not None
   head_after = subprocess.run(['git', '-C', tmpdir, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
   assert head_after == result.sha
   print('OK: empty commit created with allow_empty=True')
   "
   ```
   Expected: `OK: empty commit created with allow_empty=True`.

5. End-to-end: a dirty working tree + commit → normal commit created:
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess
   tmpdir = tempfile.mkdtemp()
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 'test@test.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'Test'], check=True)
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)
   Path(tmpdir, 'new.txt').write_text('new file content')
   subprocess.run(['git', '-C', tmpdir, 'add', 'new.txt'], check=True)
   from utils import git_ops
   result = git_ops.commit(tmpdir, 'real change')
   assert result.success is True
   assert result.sha is not None
   # Verify the commit has new.txt in its diff
   show = subprocess.run(['git', '-C', tmpdir, 'show', '--stat', result.sha], check=True, capture_output=True, text=True).stdout
   assert 'new.txt' in show
   print('OK: normal commit created with file in diff')
   "
   ```
   Expected: `OK: normal commit created with file in diff`.

6. Full test suite (regression):
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -5
   ```
   Expected: 1547+ passed (1547 + new tests), 1 skipped, exit 0. If existing tests that called `commit()` on a clean working tree now fail, those tests need `allow_empty=True` added — note this in the report.

## Deliverable

- Both edits applied
- All 6 verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: added allow_empty parameter and empty-check to commit() — line N in utils/git_ops.py, evidence: V1 output
- [x] Edit 2: added 3+ tests for the new behavior — line N in tests/test_git_ops.py, evidence: V2 output
- [x] Verification 1: function has allow_empty parameter — <paste output>
- [x] Verification 2: new tests pass — <paste pytest output>
- [x] Verification 3: clean tree + commit → refused — <paste output>
- [x] Verification 4: clean tree + allow_empty=True → empty commit — <paste output>
- [x] Verification 5: dirty tree + commit → normal commit — <paste output>
- [x] Verification 6: full test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```
