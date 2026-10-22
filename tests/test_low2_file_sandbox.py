# tests/test_low2_file_sandbox.py
# Tests for LOW-2: file tools default sandbox to /tmp fix.
#
# BUG #1 regression: read_file("src/a.py") must resolve under conv.project_path,
#   NOT under <project>/.crabcakes/tmp/<session>/src/a.py.
#   File tools ignore scratch_dir; exec_command uses scratch_dir as cwd.
#
# BUG #2: session_key must be validated to prevent empty keys and path escapes.

import os
import stat
import tempfile

import pytest

from agent.persistence import resolve_session_workspace
from agent.tools import execute_tool


# ═══════════════════════════════════════════════════════════════════
#  BUG #2 — session_key validation
# ═══════════════════════════════════════════════════════════════════

class TestSessionKeyValidation:
    """BUG #2: resolve_session_workspace must reject malformed session_key."""

    def test_empty_project_path_raises(self):
        """LOW-2: empty project_path must raise ValueError, NOT fall back to /tmp."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("", "session_x")

    def test_none_project_path_raises(self):
        """LOW-2: None project_path must raise ValueError."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace(None, "session_x")

    def test_empty_session_key_raises(self):
        """BUG #2: empty session_key must raise ValueError (prevents root-level collison dir)."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("/proj", "")

    def test_whitespace_only_session_key_raises(self):
        """BUG #2: whitespace-only session_key must raise ValueError."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("/proj", "   ")

    def test_session_key_with_dotdot_raises(self):
        """BUG #2: session_key containing '..' must raise ValueError (path traversal)."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("/proj", "../escape")

    def test_session_key_with_dotdot_in_middle_raises(self):
        """BUG #2: session_key with '..' anywhere must raise ValueError."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("/proj", "foo..bar")

    def test_session_key_with_slash_raises(self):
        """BUG #2: session_key containing '/' must raise ValueError."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("/proj", "sess/sub")

    def test_session_key_with_backslash_raises(self):
        """BUG #2: session_key containing '\\' must raise ValueError."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("/proj", "sess\\sub")

    def test_session_key_with_up_level_raises(self):
        """BUG #2: session_key with '..' escaping .crabcakes/tmp/ must raise ValueError."""
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("/proj", "..")

    def test_valid_session_key_ok(self):
        """Valid [a-zA-Z0-9._:-]+ session_key works without error."""
        project = tempfile.mkdtemp()
        # These must not raise
        ws = resolve_session_workspace(project, "valid-sess_123.abc")
        assert ws.endswith("valid-sess_123.abc")

    def test_special_agent_colon_key_ok(self):
        """Session keys with colons (e.g. 'agent:coder') must work.

        The colon is sanitized to '-' in the filesystem path but the
        session_key passes validation.
        """
        project = tempfile.mkdtemp()
        ws = resolve_session_workspace(project, "agent:coder")
        # Colon is sanitized for filesystem safety
        assert ws.endswith("agent-coder")
        assert ":" not in ws.split(".crabcakes")[-1]  # no colon in the relative path portion
        assert os.path.isdir(ws)


# ═══════════════════════════════════════════════════════════════════
#  BUG #2 — workspace properties
# ═══════════════════════════════════════════════════════════════════

class TestWorkspaceProperties:
    def test_workspace_created_under_project_crabcakes_tmp(self, tmp_path):
        """Workspace must be at <project>/.crabcakes/tmp/<session_key>."""
        project = tmp_path / "my_project"
        project.mkdir()
        session = "sess-abc123"

        workspace = resolve_session_workspace(str(project), session)

        expected = project / ".crabcakes" / "tmp" / session
        assert workspace == str(expected)

    def test_workspace_mode_is_0o700(self, tmp_path):
        """Workspace directory must have 0o700 permissions (owner-only)."""
        project = tmp_path / "secure_project"
        project.mkdir()
        session = "sess-perm-test"

        workspace = resolve_session_workspace(str(project), session)

        mode = os.stat(workspace).st_mode & 0o777
        assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"

    def test_idempotent_workspace_same_path(self, tmp_path):
        """Calling twice with same session_key returns the same path."""
        project = tmp_path / "idempotent_project"
        project.mkdir()
        session = "sess-idempotent"

        ws1 = resolve_session_workspace(str(project), session)
        ws2 = resolve_session_workspace(str(project), session)

        assert ws1 == ws2

    def test_different_sessions_get_different_workspaces(self, tmp_path):
        """Different session_keys get different workspace dirs."""
        project = tmp_path / "multi_project"
        project.mkdir()

        ws_a = resolve_session_workspace(str(project), "session_a")
        ws_b = resolve_session_workspace(str(project), "session_b")

        assert ws_a != ws_b

    def test_workspace_parent_intermediate_dirs_created(self, tmp_path):
        """Intermediate .crabcakes/tmp/<session> dirs are all created."""
        project = tmp_path / "nested_project"
        project.mkdir()
        session = "sess-nested"

        workspace = resolve_session_workspace(str(project), session)

        assert os.path.isdir(workspace)
        assert os.path.isdir(os.path.dirname(workspace))  # tmp/
        assert os.path.isdir(os.path.join(str(project), ".crabcakes"))  # .crabcakes/


# ═══════════════════════════════════════════════════════════════════
#  BUG #1 regression — file tools must use project_path, not scratch_dir
# ═══════════════════════════════════════════════════════════════════

class TestFileToolsUseProjectPathNotScratchDir:
    """BUG #1 regression: read_file("src/a.py") must resolve under project_path.

    Previously, runtime.py passed the scratch workspace as project_path to execute_tool,
    causing read_file to search <project>/.crabcakes/tmp/<session>/src/a.py instead of
    <project>/src/a.py.  The fix separates sandbox base (project_path) from scratch
    directory (scratch_dir): file tools ignore scratch_dir, exec_command uses it as cwd.
    """

    def test_read_file_resolves_relative_to_project_path_not_scratch_dir(self, tmp_path):
        """read_file('a.py') searches <project>/a.py, NOT <scratch>/a.py."""
        project = tmp_path / "bug1_proj"
        project.mkdir()
        session = "sess-bug1-read"

        # Create file ONLY in the project directory
        src_dir = project / "src"
        src_dir.mkdir()
        test_file = src_dir / "a.py"
        test_file.write_text('print("hello from project")')

        # scratch_dir is completely separate — no src/ inside it
        scratch = resolve_session_workspace(str(project), session)
        assert not os.path.exists(os.path.join(scratch, "src"))

        # Call read_file with project_path = project dir, scratch_dir = workspace
        result = execute_tool(
            "read_file",
            {"path": "src/a.py"},
            str(project),          # project_path — sandbox base
            session,
            scratch_dir=scratch,  # scratch — NOT used by read_file
        )

        assert result.success is True, f"read_file failed: {result.error}"
        assert "hello from project" in result.output

    def test_read_file_does_not_find_file_in_scratch_dir(self, tmp_path):
        """If same-named file exists in scratch_dir but NOT in project, read_file fails.

        This confirms scratch_dir is NOT consulted for file resolution.
        """
        project = tmp_path / "bug1_proj2"
        project.mkdir()
        session = "sess-bug1-scratch"

        # Create file ONLY in the scratch workspace (NOT in project)
        scratch = resolve_session_workspace(str(project), session)
        scratch_file = os.path.join(scratch, "wrong.txt")
        with open(scratch_file, "w") as fh:
            fh.write("this should NOT be readable")

        # read_file with project_path = project (no wrong.txt there)
        result = execute_tool(
            "read_file",
            {"path": "wrong.txt"},
            str(project),
            session,
            scratch_dir=scratch,
        )

        # Must fail because wrong.txt does not exist in project dir
        assert result.success is False, (
            "read_file found file in scratch_dir — scratch_dir is leaking into sandbox"
        )

    def test_write_file_writes_to_project_not_scratch(self, tmp_path):
        """write_file('b.txt', ...) writes to <project>/b.txt, NOT <scratch>/b.txt."""
        project = tmp_path / "bug1_proj3"
        project.mkdir()
        session = "sess-bug1-write"

        scratch = resolve_session_workspace(str(project), session)

        result = execute_tool(
            "write_file",
            {"path": "b.txt", "content": "written to project"},
            str(project),
            session,
            scratch_dir=scratch,
        )

        assert result.success is True

        # File must exist in project dir
        assert (project / "b.txt").exists()
        assert (project / "b.txt").read_text() == "written to project"

        # File must NOT exist in scratch dir
        assert not os.path.exists(os.path.join(scratch, "b.txt"))

    def test_list_files_lists_project_not_scratch(self, tmp_path):
        """list_files('.') lists <project>/, NOT <scratch>/."""
        project = tmp_path / "bug1_proj4"
        project.mkdir()
        session = "sess-bug1-list"

        # Create different files in project and scratch
        (project / "proj_file.txt").write_text("project")
        scratch = resolve_session_workspace(str(project), session)
        with open(os.path.join(scratch, "scratch_file.txt"), "w") as fh:
            fh.write("scratch")

        result = execute_tool(
            "list_files",
            {"path": "."},
            str(project),
            session,
            scratch_dir=scratch,
        )

        assert result.success is True
        assert "proj_file.txt" in result.output
        assert "scratch_file.txt" not in result.output, (
            "list_files is listing scratch_dir — scratch_dir is leaking into sandbox"
        )
