# tests/test_config.py
# Tests for utils/config.py — centralized path resolution.

import os
import pytest
from unittest.mock import patch


class TestGetConfigDir:
    def test_defaults_to_config_develcakes(self):
        """Default config dir is ~/.config/develcakes when XDG_CONFIG_HOME is unset.

        SPEC-11 SP1 (D4): config-dir divergence — the v2 app owns
        ~/.config/develcakes; the v1 crabcakes dir is migration source only
        (get_v1_config_dir). Was crabcakes before SPEC-11 — spec drift per
        SP1 §1c, updated with disclosure.
        """
        with patch.dict(os.environ, {}, clear=True):
            from utils.config import get_config_dir
            result = get_config_dir()
            assert "develcakes" in result
            assert result.endswith("develcakes")

    def test_respects_xdg_config_home(self):
        """$XDG_CONFIG_HOME, if set, takes precedence over ~/.config."""
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/xdg"}):
            from utils.config import get_config_dir
            # Force re-import to pick up env change
            import importlib
            import utils.config
            importlib.reload(utils.config)
            from utils.config import get_config_dir
            assert get_config_dir() == "/custom/xdg/develcakes"
            importlib.reload(utils.config)  # restore for other tests


class TestGetV1ConfigDir:
    """SPEC-11 SP1: the v1 dir getter mirrors the old crabcakes shape."""

    def test_defaults_to_config_crabcakes(self):
        with patch.dict(os.environ, {}, clear=True):
            from utils.config import get_v1_config_dir
            result = get_v1_config_dir()
            assert "crabcakes" in result
            assert result.endswith("crabcakes")

    def test_respects_xdg_config_home(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/xdg"}):
            from utils.config import get_v1_config_dir
            assert get_v1_config_dir() == "/custom/xdg/crabcakes"

    def test_getters_diverge(self):
        """The two getters must never return the same dir (else the
        migration would read and write the same tree)."""
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/xdg"}):
            from utils.config import get_config_dir, get_v1_config_dir
            assert get_config_dir() != get_v1_config_dir()


class TestGetConfigFile:
    def test_joins_config_json(self):
        """get_config_file() appends config.json to config dir."""
        with patch.dict(os.environ, {}, clear=True):
            from utils.config import get_config_file
            assert get_config_file().endswith("config.json")


class TestGetProjectsConfigDir:
    def test_joins_projects_inside_config_dir(self):
        """get_projects_config_dir() is config_dir/projects, not ~/projects."""
        with patch.dict(os.environ, {}, clear=True):
            from utils.config import get_projects_config_dir, get_config_dir
            result = get_projects_config_dir()
            assert result.endswith("projects")
            assert get_config_dir() in result


class TestGetProjectsDir:
    def test_defaults_to_home_projects(self):
        """Default projects dir is ~/projects when CRABCAKES_PROJECTS_DIR is unset."""
        with patch.dict(os.environ, {}, clear=True):
            from utils.config import get_projects_dir
            result = get_projects_dir()
            assert result.endswith("projects")

    def test_respects_crabcakes_projects_dir_env(self):
        """$CRABCAKES_PROJECTS_DIR overrides the default."""
        with patch.dict(os.environ, {"CRABCAKES_PROJECTS_DIR": "/opt/my-projects"}):
            import importlib
            import utils.config
            importlib.reload(utils.config)
            from utils.config import get_projects_dir
            assert get_projects_dir() == "/opt/my-projects"
            importlib.reload(utils.config)
