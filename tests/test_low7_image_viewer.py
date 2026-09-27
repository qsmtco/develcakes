# tests/test_low7_image_viewer.py
# Phase 5: LOW-7 image viewer path hardening.
# Tests _open_in_viewer and _is_path_in_allowed_roots in ui/views/event_cards.py.

import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest


class TestIsPathInAllowedRoots:
    """Unit tests for _is_path_in_allowed_roots (pure logic, no subprocess)."""

    def test_low7_allows_tmp_without_env_var(self):
        """When no project path is set, /tmp is allowed."""
        # Ensure the env var is not set
        os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)
        from ui.views.event_cards import _is_path_in_allowed_roots
        assert _is_path_in_allowed_roots("/tmp/somefile.png") is True

    def test_low7_rejects_etc_passwd_without_env_var(self):
        """When no project path is set, /etc/passwd is rejected."""
        os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)
        from ui.views.event_cards import _is_path_in_allowed_roots
        assert _is_path_in_allowed_roots("/etc/passwd") is False

    def test_low7_allows_project_path_when_set(self):
        """When CRABCAKES_ACTIVE_PROJECT_PATH is set, that path is allowed."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            from ui.views.event_cards import _is_path_in_allowed_roots
            file_path = os.path.join(tmp, "image.png")
            # Create the file so isfile passes in _open_in_viewer
            open(file_path, "w").close()
            assert _is_path_in_allowed_roots(file_path) is True
            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)

    def test_low7_rejects_path_outside_project(self):
        """A path outside the allowed roots is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            from ui.views.event_cards import _is_path_in_allowed_roots
            assert _is_path_in_allowed_roots("/etc/passwd") is False
            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)

    def test_low7_rejects_symlink_to_etc_passwd(self):
        """A symlink inside allowed root that points outside is rejected (realpath resolves it)."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            from ui.views.event_cards import _is_path_in_allowed_roots
            # Create a symlink inside tmp pointing to /etc/passwd
            symlink_path = os.path.join(tmp, "malicious.png")
            try:
                os.symlink("/etc/passwd", symlink_path)
            except OSError:
                pytest.skip("symlink creation not allowed on this system")
            # /etc/passwd is not in allowed roots, so the resolved path is rejected
            assert _is_path_in_allowed_roots(symlink_path) is False
            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)


class TestOpenInViewer:
    """Integration tests for _open_in_viewer — subprocess.Popen must NOT be called on blocked paths."""

    def test_low7_open_rejects_etc_passwd(self):
        """_open_in_viewer(/etc/passwd) must NOT call subprocess.Popen."""
        from ui.views.event_cards import _open_in_viewer
        with patch("subprocess.Popen") as mock_popen:
            _open_in_viewer("/etc/passwd")
            mock_popen.assert_not_called()

    def test_low7_open_rejects_nonexistent(self):
        """_open_in_viewer with nonexistent path must NOT call subprocess.Popen."""
        from ui.views.event_cards import _open_in_viewer
        with patch("subprocess.Popen") as mock_popen:
            _open_in_viewer("/tmp/does-not-exist-xyz.png")
            mock_popen.assert_not_called()

    def test_low7_open_allows_project_file(self):
        """_open_in_viewer with a real file inside project root MUST call Popen."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            file_path = os.path.join(tmp, "image.png")
            open(file_path, "w").close()

            from ui.views.event_cards import _open_in_viewer
            with patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock()
                _open_in_viewer(file_path)
                mock_popen.assert_called_once()
                args = mock_popen.call_args[0][0]
                assert file_path in args[-1]

            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)

    def test_low7_open_allows_tmp_file(self):
        """_open_in_viewer with a real file in /tmp MUST call Popen."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"fake image")
            file_path = f.name
        try:
            from ui.views.event_cards import _open_in_viewer
            with patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock()
                _open_in_viewer(file_path)
                mock_popen.assert_called_once()
                args = mock_popen.call_args[0][0]
                assert file_path in args[-1]
        finally:
            os.unlink(file_path)

    def test_low7_open_rejects_symlink_outside_root(self):
        """A symlink inside project pointing to /etc/passwd must NOT open."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            symlink_path = os.path.join(tmp, "malicious.png")
            try:
                os.symlink("/etc/passwd", symlink_path)
            except OSError:
                os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)
                pytest.skip("symlink creation not allowed on this system")
            from ui.views.event_cards import _open_in_viewer
            with patch("subprocess.Popen") as mock_popen:
                _open_in_viewer(symlink_path)
                mock_popen.assert_not_called()
            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)

    def test_low7_open_allows_home_file(self):
        """_open_in_viewer with a real file in home directory MUST call Popen."""
        home_file = os.path.join(os.path.expanduser("~"), ".crabcakes-test-image.png")
        open(home_file, "w").close()
        try:
            from ui.views.event_cards import _open_in_viewer
            with patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock()
                _open_in_viewer(home_file)
                mock_popen.assert_called_once()
                args = mock_popen.call_args[0][0]
                assert home_file in args[-1]
        finally:
            os.unlink(home_file)
