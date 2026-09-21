# Phase T2-RL3 — feed_handler fix: message from staged files + empty handling

**Source:** `docs/post-mortems/2026-06-16-REVIEW-LAYER-INVESTIGATION.md` (third caller: feed_handler.py:573)
**Depends on:** T2-RL1 (commit() has allow_empty), T2-RL2 (pattern is established)
**Severity:** MEDIUM (same class of bug as T2-RL2 — static message instead of actual files)
**Risk:** Low
**Lines:** +25 in `ui/handlers/feed_handler.py`, +50 in tests

## Goal

Fix the third buggy caller of `git_ops.commit()`: `feed_handler.py:573` (`_git_accept` closure inside `handle_accept`). The current code uses a static message `f"Accept: {card.title}"` which:
- Names one file (the card's title) even when multiple files were changed
- May not match the actual diff (the card's title is user-facing, not a git file path)
- After T2-RL1, also creates empty commits when the working tree is clean (because it doesn't pass `allow_empty=True` and the working tree is often clean at this point in the flow)

Fix: generate the commit message from the actual staged files (same pattern as T2-RL2). Handle the empty case as a silent no-op (no commit, no chat message — just a log line, because the user clicked on a card and the card remains visible).

## Files to change

1. `ui/handlers/feed_handler.py` — fix the `_git_accept` closure
2. `tests/test_feed_handler.py` — add 3 new tests for the new behavior

## Edit 1: `ui/handlers/feed_handler.py` — fix `_git_accept`

**Anchor:** the `_git_accept` closure inside `handle_accept` (line 567-585). Find this pattern:

```python
            def _git_accept():
                result_stage = git_ops.stage_all(project_path)
                if not result_stage.success:
                    _logger.warning("handle_accept: git stage failed for %s", project_path)
                    return
                commit_msg = f"Accept: {card.title}"
                result_commit = git_ops.commit(project_path, commit_msg)
                if result_commit.success:
                    card.accepted = True
                    card.metadata["project_path"] = project_path
                    # Persist to feed.json
                    feed_store.update_feed_card(project_path, card_id, {"accepted": True})
                    # Update visual on main thread
                    def _mark():
                        self._update_card_visual(card_id, accepted=True)
                    self._GLib.idle_add(_mark)
                    self._GLib.idle_add(lambda: self._add_git_card(card, result_commit))
```

**Replace with:**

```python
            def _git_accept():
                result_stage = git_ops.stage_all(project_path)
                if not result_stage.success:
                    _logger.warning("handle_accept: git stage failed for %s", project_path)
                    return

                # Generate the commit message from the ACTUAL staged files,
                # not from card.title. card.title is user-facing text (not a
                # file path) and may not match the real diff. Same fix as
                # T2-RL2 in review_handler.
                #
                # Only catch ImportError (gitpython not installed). Other
                # exceptions are logged as warnings — the user clicked Accept
                # on a card and the card remains visible, so we don't need
                # to surface a chat message like T2-RL2 does.
                try:
                    import git as gitpython
                except ImportError:
                    staged = []
                else:
                    try:
                        repo = gitpython.Repo(project_path)
                        staged = repo.index.diff("HEAD")
                    except Exception as e:
                        _logger.warning(
                            "handle_accept: failed to read diff for %s: %s: %s",
                            project_path, type(e).__name__, e,
                        )
                        return

                if not staged:
                    # Working tree is clean — nothing to commit. Silent no-op:
                    # the user clicked Accept on a card but the underlying
                    # changes have already been accepted (or never existed).
                    # Log a warning for observability, but don't create an
                    # empty commit and don't mark the card as accepted.
                    _logger.info(
                        "handle_accept: nothing to commit for card %s (working tree clean)",
                        card_id,
                    )
                    return

                # Build a descriptive message from the actual files
                file_list = sorted({d.a_path or d.b_path for d in staged if d.a_path or d.b_path})
                if len(file_list) == 1:
                    commit_msg = f"Accept: {file_list[0]}"
                elif len(file_list) <= 3:
                    commit_msg = f"Accept: {len(file_list)} files ({', '.join(file_list)})"
                else:
                    commit_msg = f"Accept: {len(file_list)} files ({', '.join(file_list[:3])}...)"

                result_commit = git_ops.commit(project_path, commit_msg)
                if result_commit.success:
                    card.accepted = True
                    card.metadata["project_path"] = project_path
                    # Persist to feed.json
                    feed_store.update_feed_card(project_path, card_id, {"accepted": True})
                    # Update visual on main thread
                    def _mark():
                        self._update_card_visual(card_id, accepted=True)
                    self._GLib.idle_add(_mark)
                    self._GLib.idle_add(lambda: self._add_git_card(card, result_commit))
```

**Key changes:**

1. After `stage_all`, get the actual diff from `repo.index.diff("HEAD")` (with try/except for safety, ImportError as graceful fallback to empty list, other exceptions logged and return)
2. If diff is empty, log an info message and return (no commit, no card acceptance)
3. If diff has changes, generate the message from the file list (1 file: "Accept: X"; 2-3: "Accept: N files (a, b)"; 4+: "Accept: N files (a, b, c...)")
4. The card's title is no longer used for the commit message (it's user-facing text, not a file path)
5. The success path (mark card accepted, persist, update visual) is unchanged

**Note:** the message format here uses `f"Accept: {file_list[0]}"` (not `f"[review] accepted: Accept: Modified X"` like T2-RL2). The feed handler's commits don't have the `[review]` prefix because they're not review-layer commits — they're feed-card-triggered accepts. The format is `Accept: <file(s)>` to match the existing convention.

## Edit 2: `tests/test_feed_handler.py` — add 3 new tests

**Anchor:** append new test methods to the existing test class (or create a new class). Look at the existing style in the file to determine the right placement.

Recommended tests (follow the existing style):

```python
class TestHandleAccept:
    """handle_accept should commit the actual staged files, not the card title."""

    @patch("ui.handlers.feed_handler.git_ops")
    @patch("ui.handlers.feed_handler.feed_store")
    def test_handle_accept_uses_staged_files_for_commit_message(
        self, mock_feed_store, mock_git_ops, feed_handler
    ):
        """When accepting a feed card, the commit message should be derived
        from the actual staged files, not from card.title.

        Regression test for review-layer fix T2-RL3.
        """
        # Set up a feed card
        card = feed_handler.add_card(
            project_name="testproject",
            card_type="file_modified",
            title="Modified src/main.py",  # user-facing title
            file_path="src/main.py",
            metadata={},
        )
        card_id = card.card_id

        # Mock git_ops: stage succeeds, commit succeeds
        mock_git_ops.stage_all.return_value = MockGitResult(success=True)
        mock_git_ops.commit.return_value = MockGitResult(
            success=True, stdout="[main abc123d] Accept: src/main.py", sha="abc123def456"
        )

        # Mock gitpython import to return a staged list with a different file
        import sys
        mock_git_module = MagicMock()
        mock_diff = MagicMock()
        mock_diff.a_path = "src/other.py"  # different from card.title
        mock_diff.b_path = None
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = [mock_diff]
        mock_git_module.Repo.return_value = mock_repo

        project_path = "/tmp/testproject"
        feed_handler._project_paths["testproject"] = project_path

        with patch.dict(sys.modules, {"git": mock_git_module}):
            feed_handler.handle_accept(card_id)

        # Verify commit was called with the ACTUAL file, not card.title
        commit_call = mock_git_ops.commit.call_args
        assert commit_call is not None
        commit_msg = commit_call[0][1]  # second positional arg
        assert "src/other.py" in commit_msg
        assert "Modified" not in commit_msg  # the user-facing title is NOT in the message

    @patch("ui.handlers.feed_handler.git_ops")
    @patch("ui.handlers.feed_handler.feed_store")
    def test_handle_accept_empty_tree_silently_noops(
        self, mock_feed_store, mock_git_ops, feed_handler
    ):
        """When the working tree is clean (no staged files), handle_accept
        should be a silent no-op. The card is not marked accepted and no
        empty commit is created.
        """
        card = feed_handler.add_card(
            project_name="testproject",
            card_type="file_modified",
            title="Modified src/main.py",
            file_path="src/main.py",
            metadata={},
        )
        card_id = card.card_id

        mock_git_ops.stage_all.return_value = MockGitResult(success=True)
        mock_git_ops.commit.return_value = MockGitResult(success=True)

        # Mock gitpython to return empty staged list (clean working tree)
        import sys
        mock_git_module = MagicMock()
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = []  # empty
        mock_git_module.Repo.return_value = mock_repo
        project_path = "/tmp/testproject"
        feed_handler._project_paths["testproject"] = project_path

        with patch.dict(sys.modules, {"git": mock_git_module}):
            feed_handler.handle_accept(card_id)

        # commit() should NOT have been called (no staged changes)
        mock_git_ops.commit.assert_not_called()
        # The card should NOT be marked accepted
        assert card.accepted is False

    @patch("ui.handlers.feed_handler.git_ops")
    @patch("ui.handlers.feed_handler.feed_store")
    def test_handle_accept_multi_file_message(
        self, mock_feed_store, mock_git_ops, feed_handler
    ):
        """When multiple files are staged, the commit message should list
        all of them (up to 3 inline, then '...' for more).
        """
        card = feed_handler.add_card(
            project_name="testproject",
            card_type="file_modified",
            title="Modified src/main.py",
            file_path="src/main.py",
            metadata={},
        )
        card_id = card.card_id

        mock_git_ops.stage_all.return_value = MockGitResult(success=True)
        mock_git_ops.commit.return_value = MockGitResult(
            success=True, stdout="[main abc123d] multi", sha="abc123"
        )

        # Mock gitpython to return multiple staged files
        import sys
        mock_git_module = MagicMock()
        mock_diffs = []
        for fname in ["src/main.py", "src/utils.py", "tests/test_main.py"]:
            mock_diff = MagicMock()
            mock_diff.a_path = fname
            mock_diff.b_path = None
            mock_diffs.append(mock_diff)
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = mock_diffs
        mock_git_module.Repo.return_value = mock_repo
        project_path = "/tmp/testproject"
        feed_handler._project_paths["testproject"] = project_path

        with patch.dict(sys.modules, {"git": mock_git_module}):
            feed_handler.handle_accept(card_id)

        # Verify commit was called with a message that lists multiple files
        commit_call = mock_git_ops.commit.call_args
        commit_msg = commit_call[0][1]
        assert "3 files" in commit_msg
        assert "src/main.py" in commit_msg
        assert "src/utils.py" in commit_msg
        assert "tests/test_main.py" in commit_msg
```

**Note:** the test patterns use `MockGitResult` and `feed_handler` fixtures. Read `tests/test_feed_handler.py` to find the exact fixture names and adjust as needed. The existing file may use different fixture names or class structure.

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do NOT modify any other function in `feed_handler.py` (e.g., `add_card`, `remove_card`, `clear_project`).
- Do NOT change the `_add_git_card` call or the card acceptance flow.
- Do NOT change the error logging pattern (`_logger.warning`, `_logger.info`).
- The empty-tree case is a silent no-op (log only, no chat message). This is different from T2-RL2 (which shows "Nothing to commit" in the chat). The reason: handle_accept is triggered by clicking a card, not by a review session. The user sees the card remain; they don't need a chat message.
- The message format `Accept: <file(s)>` matches the existing convention (no `[review]` prefix because these are feed-card commits, not review commits).

## Verification (run yourself, paste output in report)

1. The `_git_accept` closure no longer uses `card.title` for the commit:
   ```
   grep -n "f\"Accept: {card.title}\"" ui/handlers/feed_handler.py
   ```
   Expected: 0 matches (the old pattern is gone).

2. The new staged-files pattern is in place:
   ```
   grep -n "file_list\|repo.index.diff" ui/handlers/feed_handler.py
   ```
   Expected: at least 2 matches.

3. The new tests pass:
   ```
   python3 -m pytest tests/test_feed_handler.py::TestHandleAccept -v 2>&1 | tail -10
   ```
   Expected: 3 tests pass.

4. End-to-end: card click on a dirty tree → commit with right file in message:
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess
   tmpdir = tempfile.mkdtemp()
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 't@t.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'T'], check=True)
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)
   Path(tmpdir, 'changed.py').write_text('# new')
   from utils import git_ops
   stage_result = git_ops.stage_all(tmpdir)
   assert stage_result.success
   commit_result = git_ops.commit(tmpdir, 'Accept: changed.py')
   assert commit_result.success
   log = subprocess.run(['git', '-C', tmpdir, 'log', '-1', '--pretty=%s'], check=True, capture_output=True, text=True).stdout.strip()
   assert 'changed.py' in log
   print(f'OK: feed-style accept committed with right file: {log!r}')
   "
   ```
   Expected: `OK: feed-style accept committed with right file: ...`.

5. End-to-end: card click on a clean tree → no commit:
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess
   tmpdir = tempfile.mkdtemp()
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 't@t.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'T'], check=True)
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)
   from utils import git_ops
   stage_result = git_ops.stage_all(tmpdir)
   commit_result = git_ops.commit(tmpdir, 'Accept: should refuse')
   assert commit_result.success is False
   assert 'nothing to commit' in commit_result.error
   head = subprocess.run(['git', '-C', tmpdir, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
   log_count = subprocess.run(['git', '-C', tmpdir, 'log', '--oneline'], check=True, capture_output=True, text=True).stdout.count('\\n')
   assert log_count == 1, f'expected only initial commit, got: {log_count} commits'
   print('OK: clean tree + accept refused, no commit created')
   "
   ```
   Expected: `OK: clean tree + accept refused, no commit created`.

6. Full test suite (regression):
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -5
   ```
   Expected: 1551+ passed (1551 + 3 new = 1554), 1 skipped, exit 0.

## Deliverable

- Edit 1 applied (the `_git_accept` fix)
- Edit 2 applied (3 new tests)
- All 6 verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: replaced _git_accept closure with staged-files message generation — line N in ui/handlers/feed_handler.py, evidence: V1 + V2 output
- [x] Edit 2: added TestHandleAccept class with 3 tests — line N in tests/test_feed_handler.py, evidence: V3 output
- [x] Verification 1: old card.title pattern is gone — <paste output>
- [x] Verification 2: new staged-files pattern is in place — <paste output>
- [x] Verification 3: new tests pass — <paste pytest output>
- [x] Verification 4: dirty tree + accept commits with right file — <paste output>
- [x] Verification 5: clean tree + accept refuses — <paste output>
- [x] Verification 6: full test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```
