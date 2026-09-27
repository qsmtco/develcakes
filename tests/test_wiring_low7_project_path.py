# tests/test_wiring_low7_project_path.py
# Phase 5+ follow-up: tests for the LOW-7 active-project-path wiring helpers
# in ui/wiring.py. These cover the setter/clearer that ui/window.py invokes
# from the project open/close callbacks.
#
# This closes the gap QTR flagged in the Phase 5 audit: event_cards.py reads
# CRABCAKES_ACTIVE_PROJECT_PATH, but no handler was setting it. Now window.py
# sets it on project open and clears it on close.

import os

import pytest

from ui.wiring import (
    ACTIVE_PROJECT_ENV,
    set_active_project_path,
    clear_active_project_path,
)


@pytest.fixture
def clean_env():
    """Ensure the env var is unset before and after the test."""
    os.environ.pop(ACTIVE_PROJECT_ENV, None)
    yield
    os.environ.pop(ACTIVE_PROJECT_ENV, None)


class TestSetActiveProjectPath:
    def test_sets_env_var_to_path(self, clean_env, tmp_path):
        """set_active_project_path stores the path in the env var."""
        set_active_project_path(str(tmp_path))
        assert os.environ[ACTIVE_PROJECT_ENV] == str(tmp_path)

    def test_overwrites_prior_value(self, clean_env, tmp_path):
        """A second call replaces the first; no accumulation."""
        other = tmp_path / "other"
        other.mkdir()
        set_active_project_path(str(tmp_path))
        set_active_project_path(str(other))
        assert os.environ[ACTIVE_PROJECT_ENV] == str(other)

    def test_empty_path_does_not_set_env(self, clean_env, caplog):
        """Empty path is a no-op (logs a warning)."""
        with caplog.at_level("WARNING"):
            set_active_project_path("")
        assert ACTIVE_PROJECT_ENV not in os.environ

    def test_tilde_is_expanded(self, clean_env):
        """A path starting with ~ is expanded to the user's home.

        Required for event_cards.py's _is_path_in_allowed_roots to work,
        since os.path.realpath does NOT expand ~. Without this, the env
        var would store '~/foo' literally and no realpath comparison
        would match.
        """
        set_active_project_path("~/my-project")
        stored = os.environ[ACTIVE_PROJECT_ENV]
        assert "~" not in stored
        assert stored == os.path.expanduser("~/my-project")

    def test_relative_path_is_made_absolute(self, clean_env, tmp_path, monkeypatch):
        """A relative path is resolved against cwd to an absolute path."""
        monkeypatch.chdir(tmp_path)
        set_active_project_path("my-project")
        stored = os.environ[ACTIVE_PROJECT_ENV]
        assert os.path.isabs(stored)
        assert stored == os.path.join(str(tmp_path), "my-project")


class TestClearActiveProjectPath:
    def test_clears_when_set(self, clean_env, tmp_path):
        """clear removes the env var if it was set."""
        set_active_project_path(str(tmp_path))
        assert ACTIVE_PROJECT_ENV in os.environ
        clear_active_project_path()
        assert ACTIVE_PROJECT_ENV not in os.environ

    def test_clear_is_safe_when_unset(self, clean_env):
        """clear is a no-op if the env var was never set (no KeyError)."""
        # Should not raise
        clear_active_project_path()
        assert ACTIVE_PROJECT_ENV not in os.environ


class TestRoundTrip:
    def test_set_then_clear_returns_to_unset(self, clean_env, tmp_path):
        """Open then close leaves the env var in the same state as before."""
        assert ACTIVE_PROJECT_ENV not in os.environ
        set_active_project_path(str(tmp_path))
        assert ACTIVE_PROJECT_ENV in os.environ
        clear_active_project_path()
        assert ACTIVE_PROJECT_ENV not in os.environ
