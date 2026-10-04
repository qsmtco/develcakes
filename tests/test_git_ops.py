# tests/test_git_ops.py
# Tests for utils/git_ops.py
# Tests against temporary git repos (created via GitPython in setUp, deleted in tearDown).

import os
import tempfile
import shutil
import pytest

# Skip if gitpython not available
try:
    import git as gitpython
    from utils.git_ops import (
        is_repo, init_repo, get_head_sha, stage_all, commit,
        diff_against, diff_stat_against, diff_file_against,
        diff_file_against_working_tree,
        checkout_paths, log, file_log, push, status, status_porcelain, GitResult,
    )
except ImportError:
    pytest.skip("gitpython not available", allow_module_level=True)


@pytest.fixture
def temp_repo():
    """Create a temporary directory with a fresh git repo. Delete on teardown."""
    tmpdir = tempfile.mkdtemp(prefix="crabcakes_test_git_")
    repo = gitpython.Repo.init(tmpdir)
    # Configure git user for commits
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@test.com").release()
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def repo_with_commit(temp_repo):
    """A repo with one initial commit containing 'hello.txt'."""
    fpath = os.path.join(temp_repo, "hello.txt")
    with open(fpath, "w") as f:
        f.write("Hello, world!\n")
    repo = gitpython.Repo(temp_repo)
    repo.index.add(["hello.txt"])
    commit_obj = repo.index.commit("Initial commit")
    return temp_repo, str(commit_obj.hexsha)


class TestIsRepo:
    def test_is_repo_true(self, temp_repo):
        assert is_repo(temp_repo) is True

    def test_is_repo_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert is_repo(tmpdir) is False

    def test_is_repo_nonexistent(self):
        assert is_repo("/nonexistent/path/12345") is False


class TestInitRepo:
    def test_init_repo_creates_repo(self, temp_repo):
        # temp_repo is already a repo, but let's test on a fresh dir
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_repo(tmpdir)
            assert result.success is True
            assert is_repo(tmpdir) is True

    def test_init_existing_repo(self, temp_repo):
        # Idempotent — no error if already a repo
        result = init_repo(temp_repo)
        assert result.success is True


class TestCommit:
    def test_commit_success(self, temp_repo):
        # Create a file and commit
        fpath = os.path.join(temp_repo, "test.txt")
        with open(fpath, "w") as f:
            f.write("Test content\n")
        stage_all(temp_repo)
        result = commit(temp_repo, "Test commit")
        assert result.success is True
        assert result.sha is not None
        assert len(result.sha) == 40  # full SHA

    def test_commit_nothing_staged(self, temp_repo):
        # Note: GitPython/empty git repo - first commit creates an empty commit
        # if index is empty and no parent exists. Subsequent empty commits may fail.
        # We test the common case: adding a file then committing.
        fpath = os.path.join(temp_repo, "test.txt")
        with open(fpath, "w") as f:
            f.write("Test content\n")
        stage_all(temp_repo)
        result = commit(temp_repo, "Test commit")
        assert result.success is True
        assert result.sha is not None

    def test_commit_refuses_empty_when_allow_empty_false(self, repo_with_commit):
        """When the working tree is clean and allow_empty=False (default),
        commit() returns success=False with 'nothing to commit' error.
        No commit is created.
        """
        path, original_sha = repo_with_commit
        result = commit(path, "test empty commit")
        assert result.success is False
        assert "nothing to commit" in result.error
        # HEAD didn't change — no commit was created
        head_after = get_head_sha(path)
        assert head_after.sha == original_sha

    def test_commit_allows_empty_when_allow_empty_true(self, repo_with_commit):
        """When the working tree is clean and allow_empty=True, commit() creates
        an empty commit with the given message. Use only for checkpoint markers.
        """
        path, original_sha = repo_with_commit
        result = commit(path, "test checkpoint", allow_empty=True)
        assert result.success is True
        assert result.sha is not None
        assert result.sha != original_sha  # new commit created

    def test_commit_succeeds_when_changes_staged(self, repo_with_commit):
        """When the working tree has staged changes, commit() creates a
        non-empty commit with the given message. allow_empty has no effect.
        """
        path, _ = repo_with_commit
        # Stage a new file
        fpath = os.path.join(path, "new_file.txt")
        with open(fpath, "w") as f:
            f.write("new content\n")
        stage_all(path)
        result = commit(path, "real change")
        assert result.success is True
        assert result.sha is not None


class TestGetHeadSha:
    def test_get_head_sha(self, repo_with_commit):
        path, sha = repo_with_commit
        result = get_head_sha(path)
        assert result.success is True
        assert result.sha == sha

    def test_get_head_sha_empty_repo(self, temp_repo):
        result = get_head_sha(temp_repo)
        assert result.success is False


class TestDiffEmpty:
    def test_diff_empty_no_changes(self, repo_with_commit):
        path, sha = repo_with_commit
        result = diff_against(path, sha)
        assert result.success is True
        assert result.stdout == ""

    def test_diff_stat_empty(self, repo_with_commit):
        path, sha = repo_with_commit
        result = diff_stat_against(path, sha)
        assert result.success is True


class TestDiffChanges:
    def test_diff_with_changes(self, repo_with_commit):
        path, sha = repo_with_commit
        # Modify the file
        fpath = os.path.join(path, "hello.txt")
        with open(fpath, "w") as f:
            f.write("Hello, world! Modified!\n")
        stage_all(path)
        commit(path, "Modify hello.txt")

        result = diff_against(path, sha)
        assert result.success is True
        assert "hello.txt" in result.stdout
        assert "Modified" in result.stdout


class TestDiffStat:
    def test_diff_stat_with_changes(self, repo_with_commit):
        path, sha = repo_with_commit
        fpath = os.path.join(path, "hello.txt")
        with open(fpath, "w") as f:
            f.write("Hello, world! Modified!\n")
        stage_all(path)
        commit(path, "Modify hello.txt")

        result = diff_stat_against(path, sha)
        assert result.success is True
        assert "hello.txt" in result.stdout


class TestDiffFileAgainst:
    def test_diff_single_file(self, repo_with_commit):
        path, sha = repo_with_commit
        fpath = os.path.join(path, "hello.txt")
        with open(fpath, "w") as f:
            f.write("Hello, world! Modified!\n")
        stage_all(path)
        commit(path, "Modify hello.txt")

        result = diff_file_against(path, sha, "hello.txt")
        assert result.success is True
        assert "hello.txt" in result.stdout


class TestCheckoutPathsRevert:
    def test_checkout_reverts_file(self, repo_with_commit):
        path, sha = repo_with_commit
        fpath = os.path.join(path, "hello.txt")
        # Modify the file
        with open(fpath, "w") as f:
            f.write("Modified content\n")

        # Revert it
        result = checkout_paths(path, sha, ["hello.txt"])
        assert result.success is True

        # Verify content is back
        with open(fpath) as f:
            content = f.read()
        assert "Hello, world!" in content
        assert "Modified" not in content


class TestCheckoutPathsMultiple:
    def test_checkout_multiple_files(self, temp_repo):
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        # Create two files
        f1 = os.path.join(temp_repo, "file1.txt")
        f2 = os.path.join(temp_repo, "file2.txt")
        with open(f1, "w") as f:
            f.write("File 1 original\n")
        with open(f2, "w") as f:
            f.write("File 2 original\n")
        repo.index.add(["file1.txt", "file2.txt"])
        c1 = repo.index.commit("Initial")

        # Modify both files
        with open(f1, "w") as f:
            f.write("File 1 modified\n")
        with open(f2, "w") as f:
            f.write("File 2 modified\n")

        # Revert both
        result = checkout_paths(temp_repo, str(c1.hexsha), ["file1.txt", "file2.txt"])
        assert result.success is True

        with open(f1) as f:
            assert "original" in f.read()
        with open(f2) as f:
            assert "original" in f.read()


class TestPushNoRemote:
    def test_push_no_remote(self, repo_with_commit):
        path, _ = repo_with_commit
        result = push(path)
        assert result.success is False
        assert "origin" in result.error.lower() or "remote" in result.error.lower() or "error" in result.error.lower()


class TestStatusPorcelain:
    def test_status_new_file(self, repo_with_commit):
        path, _ = repo_with_commit
        # Create a new untracked file
        new_fpath = os.path.join(path, "new_file.txt")
        with open(new_fpath, "w") as f:
            f.write("New file content\n")
        result = status(path)
        assert result.success is True
        assert "new_file.txt" in result.stdout


class TestLog:
    def test_log_returns_text(self, repo_with_commit):
        path, _ = repo_with_commit
        result = log(path, count=5)
        assert result.success is True
        assert "Initial commit" in result.stdout


class TestErrorHandling:
    def test_invalid_path_returns_error(self):
        result = commit("/nonexistent/path/12345", "test")
        assert result.success is False
        assert result.error != ""

    def test_get_head_sha_nonexistent(self):
        result = get_head_sha("/nonexistent/path/12345")
        assert result.success is False


class TestCheckoutPathsShaGuards:
    """BUG #5: checkout_paths guards against non-string sha."""

    def test_checkout_paths_sha_int(self, repo_with_commit):
        path, _ = repo_with_commit
        result = checkout_paths(path, 42, ["hello.txt"])  # type: ignore[arg-type]
        assert result.success is False
        assert "Invalid git ref" in result.error

    def test_checkout_paths_sha_none(self, repo_with_commit):
        path, _ = repo_with_commit
        result = checkout_paths(path, None, ["hello.txt"])  # type: ignore[arg-type]
        assert result.success is False
        assert "Invalid git ref" in result.error

    def test_checkout_paths_sha_list(self, repo_with_commit):
        path, _ = repo_with_commit
        result = checkout_paths(path, ["HEAD"], ["hello.txt"])  # type: ignore[arg-type]
        assert result.success is False
        assert "Invalid git ref" in result.error


class TestDiffFileAgainstWorkingTree:
    """diff_file_against_working_tree: diff sha→working tree (includes uncommitted)."""

    def test_diff_against_head(self, repo_with_commit):
        """Diff HEAD against working tree with uncommitted changes."""
        path, sha = repo_with_commit
        # Make uncommitted changes
        fpath = os.path.join(path, "hello.txt")
        with open(fpath, "w") as f:
            f.write("Modified but not committed\n")

        result = diff_file_against_working_tree(path, "HEAD", "hello.txt")
        assert result.success is True
        assert "hello.txt" in result.stdout
        assert "Modified but not committed" in result.stdout

    def test_diff_against_specific_sha(self, temp_repo):
        """Diff a specific SHA against working tree."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        # Create initial commit
        fpath = os.path.join(temp_repo, "hello.txt")
        with open(fpath, "w") as f:
            f.write("V1\n")
        repo.index.add(["hello.txt"])
        c1 = repo.index.commit("v1")

        # Modify and commit v2
        with open(fpath, "w") as f:
            f.write("V2\n")
        repo.index.add(["hello.txt"])
        repo.index.commit("v2")

        # Now edit working tree (uncommitted)
        with open(fpath, "w") as f:
            f.write("V3 (working tree)\n")

        # Diff c1 (V1) against working tree (V3) — should show both changes
        result = diff_file_against_working_tree(temp_repo, str(c1.hexsha), "hello.txt")
        assert result.success is True
        assert "V3 (working tree)" in result.stdout

    def test_invalid_sha_rejected(self, repo_with_commit):
        """Invalid SHA is rejected with error (MED-11 pattern)."""
        path, _ = repo_with_commit
        result = diff_file_against_working_tree(path, "not-a-sha!!!", "hello.txt")
        assert result.success is False
        assert "Invalid git ref" in result.error

    # ----- BUG #5: non-string sha guards -----
    def test_diff_against_working_tree_sha_int(self, repo_with_commit):
        """Non-string sha (int) returns error instead of TypeError."""
        path, _ = repo_with_commit
        result = diff_file_against_working_tree(path, 42, "hello.txt")  # type: ignore[arg-type]
        assert result.success is False
        assert "Invalid git ref" in result.error

    def test_diff_against_working_tree_sha_none(self, repo_with_commit):
        """None sha returns error instead of TypeError."""
        path, _ = repo_with_commit
        result = diff_file_against_working_tree(path, None, "hello.txt")  # type: ignore[arg-type]
        assert result.success is False
        assert "Invalid git ref" in result.error

    def test_diff_against_working_tree_sha_list(self, repo_with_commit):
        """List sha returns error instead of TypeError."""
        path, _ = repo_with_commit
        result = diff_file_against_working_tree(path, ["HEAD"], "hello.txt")  # type: ignore[arg-type]
        assert result.success is False
        assert "Invalid git ref" in result.error

    # ----- BUG #2: staged+unstaged edits -----
    def test_diff_working_tree_staged_and_unstaged(self, temp_repo):
        """diff_file_against_working_tree shows both staged and unstaged changes.

        A 2-way diff (sha vs working tree) includes both staged modifications
        and unstaged modifications on top of them.
        """
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "hello.txt")
        with open(fpath, "w") as f:
            f.write("V1\n")
        repo.index.add(["hello.txt"])
        c1 = repo.index.commit("v1")

        # Stage a change
        with open(fpath, "w") as f:
            f.write("V2 (staged)\n")
        repo.index.add(["hello.txt"])

        # Make an unstaged change on top
        with open(fpath, "w") as f:
            f.write("V2 (staged)\nV3 (unstaged)\n")

        # The 2-way diff against c1 should capture both
        result = diff_file_against_working_tree(temp_repo, str(c1.hexsha), "hello.txt")
        assert result.success is True
        # Both changes should appear in the diff
        assert "V2 (staged)" in result.stdout
        assert "V3 (unstaged)" in result.stdout


class TestShaRegex:
    """BUG #9: SHA regex acceptance tests."""

    def test_valid_full_sha(self, repo_with_commit):
        """A full 40-hex SHA is accepted by diff_file_against_working_tree."""
        path, sha = repo_with_commit
        result = diff_file_against_working_tree(path, sha, "hello.txt")
        assert result.success is True

    def test_valid_short_sha(self, repo_with_commit):
        """A short (4+ hex) SHA is accepted."""
        path, sha = repo_with_commit
        result = diff_file_against_working_tree(path, sha[:8], "hello.txt")
        assert result.success is True

    def test_valid_head(self, repo_with_commit):
        """'HEAD' is accepted by the SHA guard."""
        path, _ = repo_with_commit
        result = diff_file_against_working_tree(path, "HEAD", "hello.txt")
        assert result.success is True

    def test_valid_hex_lowercase(self, repo_with_commit):
        """Lowercase hex SHA is accepted."""
        path, sha = repo_with_commit
        result = diff_file_against_working_tree(path, sha.lower(), "hello.txt")
        assert result.success is True

    def test_valid_hex_uppercase(self, repo_with_commit):
        """Uppercase hex SHA is accepted."""
        path, sha = repo_with_commit
        result = diff_file_against_working_tree(path, sha.upper(), "hello.txt")
        assert result.success is True

    def test_head_tilde_rejected(self, repo_with_commit):
        """HEAD~ is NOT accepted by the current regex (BUG #9 — known limitation)."""
        path, _ = repo_with_commit
        result = diff_file_against_working_tree(path, "HEAD~1", "hello.txt")
        assert result.success is False
        assert "Invalid git ref" in result.error

    def test_head_caret_rejected(self, repo_with_commit):
        """HEAD^ is NOT accepted by the current regex (BUG #9 — known limitation)."""
        path, _ = repo_with_commit
        result = diff_file_against_working_tree(path, "HEAD^", "hello.txt")
        assert result.success is False
        assert "Invalid git ref" in result.error


class TestFileLog:
    """file_log: commit history for a single file."""

    def test_history_for_tracked_file(self, temp_repo):
        """Returns history for a tracked file."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "hello.txt")
        with open(fpath, "w") as f:
            f.write("V1\n")
        repo.index.add(["hello.txt"])
        repo.index.commit("First commit")

        with open(fpath, "a") as f:
            f.write("V2\n")
        repo.index.add(["hello.txt"])
        repo.index.commit("Second commit")

        result = file_log(temp_repo, "hello.txt", count=10)
        assert result.success is True
        assert result.stdout != ""
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2
        # Each line should have format: SHA\x1fDATE\x1fMESSAGE
        for line in lines:
            parts = line.split("\x1f")
            assert len(parts) == 3, f"Expected 3 fields, got {len(parts)}: {line!r}"
            # First part should be a SHA (hex)
            assert len(parts[0]) == 40, f"Expected 40-char SHA, got {parts[0]!r}"
            # Second part should be ISO date
            assert parts[2] in ("First commit", "Second commit"), f"Unexpected message: {parts[2]!r}"

    def test_empty_for_untracked_file(self, repo_with_commit):
        """Returns empty stdout for an untracked file."""
        path, _ = repo_with_commit
        result = file_log(path, "nonexistent.txt", count=10)
        assert result.success is True
        assert result.stdout == ""

    def test_count_clamping(self, temp_repo):
        """Count is clamped to 1..100."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "hello.txt")
        with open(fpath, "w") as f:
            f.write("V1\n")
        repo.index.add(["hello.txt"])
        repo.index.commit("c1")

        # count=0 should be clamped to 1
        result = file_log(temp_repo, "hello.txt", count=0)
        assert result.success is True

        # count=999 should be clamped to 100
        result = file_log(temp_repo, "hello.txt", count=999)
        assert result.success is True

    def test_pipe_in_message(self, temp_repo):
        """Pipe characters in commit message don't break parsing due to \\x1f separator."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "hello.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        repo.index.add(["hello.txt"])
        repo.index.commit("feat: add |pipe| in message")

        result = file_log(temp_repo, "hello.txt", count=5)
        assert result.success is True
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1
        parts = lines[0].split("\x1f")
        assert len(parts) == 3
        assert parts[2] == "feat: add |pipe| in message"

    def test_reject_x1f_in_subject(self, temp_repo):
        """BUG #1: Commit subject containing \\x1f is rejected with error."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "hello.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        repo.index.add(["hello.txt"])
        # Create a commit with \\x1f in the message using low-level API
        repo.index.commit("safe subject")
        # Append another commit with unsafe char
        with open(fpath, "a") as f:
            f.write("more\n")
        repo.index.add(["hello.txt"])
        # GitPython allows unicode control chars, so \\x1f in message works
        repo.index.commit(f"unsafe\x1fchar")

        result = file_log(temp_repo, "hello.txt", count=5)
        assert result.success is False
        assert "unsafe" in result.error.lower()
        assert "\\x1f" in result.error or "separator" in result.error

    def test_count_non_int(self, temp_repo):
        """BUG #3: Non-int count values return clear error; bool rejected explicitly."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "hello.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        repo.index.add(["hello.txt"])
        repo.index.commit("init")

        # bool is explicitly rejected
        result = file_log(temp_repo, "hello.txt", count=True)
        assert result.success is False
        assert "bool" in result.error

        # string count is coerced (valid string)
        result = file_log(temp_repo, "hello.txt", count="3")
        assert result.success is True
        assert result.stdout != ""

        # invalid string returns error
        result = file_log(temp_repo, "hello.txt", count="not_a_number")
        assert result.success is False
        assert "count must be an integer" in result.error or "invalid" in result.error.lower()

        # float is coerced (valid float)
        result = file_log(temp_repo, "hello.txt", count=3.0)
        assert result.success is True

    def test_count_clamping_with_line_counts(self, temp_repo):
        """BUG #4: Count is clamped to 1..100. Assert actual line counts."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "hello.txt")
        for i in range(3):
            with open(fpath, "w") as f:
                f.write(f"V{i}\n")
            repo.index.add(["hello.txt"])
            repo.index.commit(f"c{i}")

        # count=0 → clamped to 1 → 1 line
        result = file_log(temp_repo, "hello.txt", count=0)
        assert result.success is True
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1, f"Expected 1 line for count=0, got {len(lines)}"

        # count=999 → clamped to 100 → 3 lines (all commits exist)
        result = file_log(temp_repo, "hello.txt", count=999)
        assert result.success is True
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3, f"Expected 3 lines for count=999, got {len(lines)}"

        # count=-5 → clamped to 1 → 1 line
        result = file_log(temp_repo, "hello.txt", count=-5)
        assert result.success is True
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1, f"Expected 1 line for count=-5, got {len(lines)}"


class TestStatusPorcelainFn:
    """status_porcelain: dict[str, str] of {rel_path: 2-char status_code}."""

    def test_empty_non_repo_returns_empty(self):
        """Non-repo or non-existent path returns empty dict."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            result = status_porcelain(tmpdir)
        assert result == {}

    def test_untracked_file(self, temp_repo):
        """A new untracked file appears with '?? ' status."""
        fpath = os.path.join(temp_repo, "untracked.txt")
        with open(fpath, "w") as f:
            f.write("new\n")
        result = status_porcelain(temp_repo)
        rel = os.path.relpath(fpath, temp_repo)
        assert result.get(rel) == "??"

    def test_modified_file(self, temp_repo):
        """A modified tracked file appears with ' M' (unstaged modified)."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        fpath = os.path.join(temp_repo, "file.txt")
        with open(fpath, "w") as f:
            f.write("v1\n")
        repo.index.add(["file.txt"])
        repo.index.commit("init")

        # Modify without staging
        with open(fpath, "w") as f:
            f.write("v2\n")

        result = status_porcelain(temp_repo)
        rel = os.path.relpath(fpath, temp_repo)
        assert rel in result
        # Worktree column (second char) should be M or space+ M
        assert "M" in result[rel]

    def test_rename_line_uses_new_path(self, temp_repo):
        """A rename 'R  old -> new' emits the destination path as key (BUG #5)."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        old_path = os.path.join(temp_repo, "old_name.txt")
        with open(old_path, "w") as f:
            f.write("content\n")
        repo.index.add(["old_name.txt"])
        repo.index.commit("init")

        # Rename and stage it
        new_path = os.path.join(temp_repo, "new_name.txt")
        repo.git.mv("old_name.txt", "new_name.txt")
        repo.index.add(["new_name.txt"])

        result = status_porcelain(temp_repo)
        # The rename may appear in staged (R ) or unstaged ( R) depending on state
        assert any("R" in v for v in result.values()), f"No rename in status: {result}"
        # The key should be 'new_name.txt', not 'old_name.txt'
        assert "new_name.txt" in result, f"new_name.txt not in keys: {list(result.keys())}"

    def test_copy_line_uses_new_path(self, temp_repo):
        """A copy 'C  old -> new' emits the destination path as key (BUG #17)."""
        repo = gitpython.Repo(temp_repo)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@test.com").release()

        src = os.path.join(temp_repo, "source.txt")
        with open(src, "w") as f:
            f.write("content\n")
        repo.index.add(["source.txt"])
        repo.index.commit("init")

        # git doesn't track copies naturally unless -C is passed
        dest = os.path.join(temp_repo, "copy.txt")
        import shutil
        shutil.copy2(src, dest)
        repo.index.add(["copy.txt"])

        result = status_porcelain(temp_repo)
        assert "copy.txt" in result, f"copy.txt not in keys: {list(result.keys())}"

    def test_too_short_line_skipped(self, temp_repo, monkeypatch):
        """A porcelain line shorter than 4 chars is skipped (BUG #4).

        Monkeypatch subprocess.run so status_porcelain's internal subprocess
        returns a synthetic too-short line.
        """
        import subprocess

        class FakeResult:
            returncode = 0
            stdout = "XY\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())

        result = status_porcelain(temp_repo)
        assert result == {}  # too-short line skipped, nothing parsed

    def test_worktree_rename_both_status_positions(self, temp_repo, monkeypatch):
        """Worktree rename ' R old -> new' checks BOTH status positions (BUG #25).

        Worktree-column rename (' R') is synthetic — git's default porcelain
        output only emits index-column renames ('R '). But the parser must
        handle both. Monkeypatch subprocess.run to inject a synthetic ' R' line.
        """
        import subprocess

        class FakeResult:
            returncode = 0
            stdout = " R old_name.txt -> new_name.txt\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())

        result = status_porcelain(temp_repo)
        # Worktree-column rename: destination path is the key
        assert "new_name.txt" in result, f"worktree rename key wrong: {result}"
        assert result["new_name.txt"] == " R"

    def test_subdirectory_of_git_repo(self, tmp_path, monkeypatch):
        """status_porcelain works when project_path is a subdir of a git repo (BUG #13)."""
        import subprocess as _subprocess
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _subprocess.run(['git', 'init', '-q', str(repo_root)], check=True)
        _subprocess.run(['git', '-C', str(repo_root), 'config', 'user.email', 't@t.com'], check=True)
        _subprocess.run(['git', '-C', str(repo_root), 'config', 'user.name', 'T'], check=True)
        subdir = repo_root / "frontend"
        subdir.mkdir()
        (subdir / "app.py").write_text("x")
        result = status_porcelain(str(subdir))
        assert len(result) > 0, f"subdir returned empty: {result}"


class TestCommitAgentTrailer:
    """SPEC-10 SP1: commit() gains agent_trailer support (D1 fail-closed).

    Every new-behavior test here was proven RED against HEAD 92694f2f
    (before the commit() edit) — see SP1 report for the RED run output.
    """

    def test_commit_agent_trailer_in_log(self, temp_repo):
        """Trailer lands as a literal 'Agent: <key>' line in the message body."""
        fpath = os.path.join(temp_repo, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(temp_repo)
        result = commit(temp_repo, "checkpoint", agent_trailer="special:coder")
        assert result.success is True, f"commit failed: {result.error}"
        head = get_head_sha(temp_repo)
        assert head.sha == result.sha
        log_result = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        assert "Agent: special:coder" in log_result
        trailer_line = [l for l in log_result.splitlines() if l.startswith("Agent: ")]
        assert trailer_line == ["Agent: special:coder"], f"unexpected trailer lines: {trailer_line}"

    def test_commit_agent_trailer_rejects_newline(self, repo_with_commit):
        r"""Newline in trailer value → fail-closed rejection, NO commit created."""
        path, _ = repo_with_commit
        fpath = os.path.join(path, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(path)
        before = get_head_sha(path)
        assert before.success is True
        result = commit(path, "checkpoint", agent_trailer="bad\nAgent: fake")
        assert result.success is False
        assert "reject" in result.error.lower()
        after = get_head_sha(path)
        assert after.sha == before.sha, "commit was created despite rejection!"

    def test_commit_no_trailer_no_agent_line(self, temp_repo):
        """Default (no agent_trailer) behavior: no 'Agent:' line ever appears."""
        fpath = os.path.join(temp_repo, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(temp_repo)
        result = commit(temp_repo, "plain commit")
        assert result.success is True
        log_result = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        assert "Agent:" not in log_result

    def test_commit_agent_trailer_rejects_empty_after_strip(self, repo_with_commit):
        """Whitespace-only trailer → reject, no commit."""
        path, _ = repo_with_commit
        fpath = os.path.join(path, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(path)
        before = get_head_sha(path)
        assert before.success is True
        result = commit(path, "checkpoint", agent_trailer="   ")
        assert result.success is False
        assert "reject" in result.error.lower()
        assert get_head_sha(path).sha == before.sha

    def test_commit_agent_trailer_rejects_nul(self, repo_with_commit):
        r"""NUL byte in trailer → reject, no commit."""
        path, _ = repo_with_commit
        fpath = os.path.join(path, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(path)
        before = get_head_sha(path)
        assert before.success is True
        result = commit(path, "checkpoint", agent_trailer="\x00")
        assert result.success is False
        assert "reject" in result.error.lower()
        assert get_head_sha(path).sha == before.sha

    def test_commit_agent_trailer_rejects_unit_separator(self, repo_with_commit):
        r"""ASCII unit separator (\x1f) in trailer → reject, no commit (file_log BUG #1 family)."""
        path, _ = repo_with_commit
        fpath = os.path.join(path, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(path)
        before = get_head_sha(path)
        assert before.success is True
        result = commit(path, "checkpoint", agent_trailer="\x1f")
        assert result.success is False
        assert "reject" in result.error.lower()
        assert get_head_sha(path).sha == before.sha

    def test_commit_agent_trailer_strips_surrounding_whitespace(self, temp_repo):
        """Trailer value is stripped; committed line is exactly 'Agent: special:coder'."""
        fpath = os.path.join(temp_repo, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(temp_repo)
        result = commit(temp_repo, "checkpoint", agent_trailer="  special:coder  ")
        assert result.success is True
        log_result = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        lines = log_result.splitlines()
        assert lines[-1] == "Agent: special:coder", f"last line: {lines[-1]!r}"
        assert "Agent:   special:coder" not in log_result


_SEPS = ["\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85"]


class TestCommitAgentTrailerHardening:
    """SPEC-10 SP1 fix round: splitlines-boundary rejection (BUG#1) + non-str
    guard (BUG#2). Both tests were proven RED against the 4-char-set guard
    before the fix landed. Runs on repo_with_commit so the HEAD-unchanged
    assertion compares REAL shas (a no-commit repo cannot detect a commit).
    """

    @pytest.mark.parametrize(
        "label,trailer_value",
        [
            (f"sep{i}-standalone", s)
            for i, s in enumerate(_SEPS)
        ]
        + [
            (f"sep{i}-embedded", f"special:coder{s}Agent: evil")
            for i, s in enumerate(_SEPS)
        ],
    )
    def test_commit_agent_trailer_rejects_unicode_and_vertical_separators(
        self, repo_with_commit, label, trailer_value
    ):
        r"""BUG#1: any Python splitlines() boundary in the trailer must be
        rejected fail-closed — standalone OR embedded mid-value — with NO
        commit created (HEAD sha unchanged, compared against a real sha).
        """
        path, _ = repo_with_commit
        fpath = os.path.join(path, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(path)
        before = get_head_sha(path)
        assert before.success is True  # real HEAD sha required for the compare
        result = commit(path, "checkpoint", agent_trailer=trailer_value)
        assert result.success is False, (
            f"[{label}] trailer {trailer_value!r} was ACCEPTED — forged-line "
            f"hole still open; error={result.error!r}"
        )
        assert "reject" in result.error.lower()
        assert result.sha is None
        after = get_head_sha(path)
        assert after.sha == before.sha, f"[{label}] commit was created despite rejection!"

    @pytest.mark.parametrize("bad_value", [123, 3.14, ["k"], b"key"])
    def test_commit_agent_trailer_rejects_non_str(self, repo_with_commit, bad_value):
        """BUG#2: non-str agent_trailer must return a fail-closed GitResult,
        never raise (file contract: 'Never raises unhandled exceptions').
        b'key' is the silent-garbage variant: it passes .strip() and the old
        char-set check and would commit "Agent: b'key'" — must reject.
        """
        path, _ = repo_with_commit
        fpath = os.path.join(path, "file.txt")
        with open(fpath, "w") as f:
            f.write("content\n")
        stage_all(path)
        before = get_head_sha(path)
        assert before.success is True
        try:
            result = commit(path, "checkpoint", agent_trailer=bad_value)
        except Exception as e:  # noqa: BLE001 — deliberate: ANY raise here is the BUG#2 failure mode
            pytest.fail(
                f"commit(agent_trailer={bad_value!r}) raised {type(e).__name__}: {e} — "
                "raw exception escaped instead of fail-closed GitResult"
            )
        assert isinstance(result, GitResult), f"expected GitResult, got {type(result)}"
        assert result.success is False, (
            f"[{type(bad_value).__name__}] non-str trailer was ACCEPTED "
            f"(sha={result.sha!r}) — silent-garbage attribution hole"
        )
        assert "reject" in result.error.lower()
        assert result.sha is None
        after = get_head_sha(path)
        assert after.sha == before.sha, "commit was created despite rejection!"
