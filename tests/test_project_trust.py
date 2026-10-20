# tests/test_project_trust.py
# Unit tests for HIGH-5 per-project trust gate.

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from utils.project_trust import (
    has_crabcakes_content,
    is_project_trusted,
    trust_project,
    untrust_project,
    request_trust_if_needed,
    set_trust_prompt_callback,
)


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Point the trust store at a tmp dir so tests are isolated.

    Patches utils.project_trust.get_config_dir (not utils.config.get_config_dir)
    because project_trust imports get_config_dir at module load time.
    """
    cfg_dir = tmp_path / "crabcakes-cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr("utils.project_trust.get_config_dir", lambda: str(cfg_dir))
    return cfg_dir


@pytest.fixture
def clean_callback():
    """Reset the global trust callback before/after each test."""
    set_trust_prompt_callback(None)
    yield
    set_trust_prompt_callback(None)


class TestHasCrabcakesContent:
    def test_no_directory_returns_false(self, tmp_path):
        assert has_crabcakes_content(str(tmp_path)) is False

    def test_empty_directory_returns_false(self, tmp_path):
        (tmp_path / ".crabcakes").mkdir()
        assert has_crabcakes_content(str(tmp_path)) is False

    def test_directory_with_non_rule_files_returns_false(self, tmp_path):
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "team.json").write_text("{}")
        (d / "context.md").write_text("hi")
        assert has_crabcakes_content(str(tmp_path)) is False

    def test_directory_with_bugs_file_returns_true(self, tmp_path):
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "coder-bugs.md").write_text("# bugs")
        assert has_crabcakes_content(str(tmp_path)) is True

    def test_directory_with_rules_file_returns_true(self, tmp_path):
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "coder-rules.md").write_text("# rules")
        assert has_crabcakes_content(str(tmp_path)) is True

    def test_directory_with_empty_bugs_file_returns_false(self, tmp_path):
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "coder-bugs.md").write_text("")
        assert has_crabcakes_content(str(tmp_path)) is False

    def test_empty_path_returns_false(self):
        assert has_crabcakes_content("") is False


class TestTrustStore:
    def test_empty_store_returns_false(self, tmp_config_dir):
        assert is_project_trusted("/anywhere") is False

    def test_trust_then_check(self, tmp_config_dir):
        path = "/tmp/test-project-1"
        trust_project(path)
        assert is_project_trusted(path) is True

    def test_untrust(self, tmp_config_dir):
        path = "/tmp/test-project-2"
        trust_project(path)
        assert is_project_trusted(path) is True
        untrust_project(path)
        assert is_project_trusted(path) is False

    def test_absolute_path_normalization(self, tmp_config_dir):
        path = "/tmp/test-project-3"
        trust_project(path)
        # The trust lookup normalizes paths via os.path.abspath, so different
        # forms of the same path should all resolve to the same entry.
        import os as _os
        # Save cwd and chdir to /
        old_cwd = _os.getcwd()
        try:
            _os.chdir("/")
            # Now './../tmp/test-project-3' resolves to '/tmp/test-project-3'
            assert is_project_trusted("./../tmp/test-project-3") is True
        finally:
            _os.chdir(old_cwd)

    def test_trust_file_permissions(self, tmp_config_dir):
        """Trust file is created with 0600 permissions (contains trust decisions)."""
        trust_project("/tmp/test-project-4")
        trust_file = tmp_config_dir / "trusted_projects.json"
        assert trust_file.exists()
        mode = trust_file.stat().st_mode & 0o777
        assert mode == 0o600, f"Trust store should be 0o600, got {oct(mode)}"

    def test_trust_file_atomic_write(self, tmp_config_dir):
        """Trust file is written via .tmp + os.replace (atomic)."""
        trust_project("/tmp/test-project-5")
        trust_file = tmp_config_dir / "trusted_projects.json"
        # .tmp file should not linger after successful write
        assert not (trust_file.with_suffix(".tmp")).exists()

    def test_corrupt_trust_file_returns_empty(self, tmp_config_dir):
        trust_file = tmp_config_dir / "trusted_projects.json"
        trust_file.write_text("not json {{{")
        # Should not raise; returns empty
        assert is_project_trusted("/anywhere") is False

    def test_non_dict_trust_file_returns_empty(self, tmp_config_dir):
        trust_file = tmp_config_dir / "trusted_projects.json"
        trust_file.write_text("[1, 2, 3]")
        assert is_project_trusted("/anywhere") is False


class TestRequestTrustIfNeeded:
    def test_no_crabcakes_skips_gate(self, tmp_config_dir, clean_callback, tmp_path):
        """If there's no .crabcakes/ content, no gate fires (returns True)."""
        result = request_trust_if_needed(str(tmp_path))
        assert result is True

    def test_untrusted_with_no_callback_returns_false(
        self, tmp_config_dir, clean_callback, tmp_path
    ):
        """HIGH-5 fail-secure: no callback + untrusted project = deny."""
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "coder-bugs.md").write_text("evil instructions")
        # No callback registered; default deny
        result = request_trust_if_needed(str(tmp_path))
        assert result is False

    def test_untrusted_with_approving_callback_returns_true(
        self, tmp_config_dir, clean_callback, tmp_path
    ):
        """User approves trust → project is recorded, ingestion proceeds."""
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "coder-bugs.md").write_text("ok")
        set_trust_prompt_callback(lambda p: True)
        result = request_trust_if_needed(str(tmp_path))
        assert result is True
        assert is_project_trusted(str(tmp_path)) is True

    def test_untrusted_with_denying_callback_returns_false(
        self, tmp_config_dir, clean_callback, tmp_path
    ):
        """User denies trust → ingestion skipped, project NOT recorded."""
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "coder-bugs.md").write_text("ok")
        set_trust_prompt_callback(lambda p: False)
        result = request_trust_if_needed(str(tmp_path))
        assert result is False
        assert is_project_trusted(str(tmp_path)) is False

    def test_already_trusted_skips_callback(
        self, tmp_config_dir, clean_callback, tmp_path
    ):
        """If project is already trusted, callback is NOT invoked."""
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "coder-bugs.md").write_text("ok")
        # Pre-trust
        trust_project(str(tmp_path))
        # Callback that would raise — must not be called
        def must_not_be_called(p):
            raise AssertionError(f"Callback should not be called for trusted project {p}")
        set_trust_prompt_callback(must_not_be_called)
        result = request_trust_if_needed(str(tmp_path))
        assert result is True


class TestComposeSystemPromptGate:
    """End-to-end: trust gate affects compose_system_prompt output."""

    def test_untrusted_project_skips_crabcakes_content(
        self, tmp_config_dir, clean_callback, tmp_path
    ):
        from utils.prompt_loader import compose_system_prompt
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "debugger-bugs.md").write_text("BUG-CONTENT-SHOULD-NOT-APPEAR")
        # No callback → fail-secure skip
        prompt = compose_system_prompt(
            agent_name="Debugger",
            agent_role="debugger",
            project_path=str(tmp_path),
        )
        assert "BUG-CONTENT-SHOULD-NOT-APPEAR" not in prompt

    def test_trusted_project_includes_crabcakes_content(
        self, tmp_config_dir, clean_callback, tmp_path
    ):
        from utils.prompt_loader import compose_system_prompt
        d = tmp_path / ".crabcakes"
        d.mkdir()
        (d / "debugger-bugs.md").write_text("BUG-CONTENT-SHOULD-APPEAR")
        trust_project(str(tmp_path))
        prompt = compose_system_prompt(
            agent_name="Debugger",
            agent_role="debugger",
            project_path=str(tmp_path),
        )
        # HIGH-5 fence wraps the content; the content itself appears inside
        assert "BUG-CONTENT-SHOULD-APPEAR" in prompt
        assert "<untrusted-project-data" in prompt
