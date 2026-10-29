# tests/test_low7_image_viewer.py
# Phase 5: LOW-7 image viewer path hardening.
# The viewer (_open_in_viewer) died with event_cards.py in SPEC-58 §2c, but
# its containment policy — is_path_in_allowed_roots in utils/image_paths.py —
# still guards render/html.py and utils/live_reader.py image blocks. These
# tests target the surviving policy directly.
#
# (The subprocess tests in TestOpenInViewer pinned event_cards' viewer
# wrapper and were deleted with it in SPEC-58 §2i; the containment cases
# they relied on live here, re-pointed at utils.image_paths.)

import os
import tempfile

import pytest


class TestIsPathInAllowedRoots:
    """Unit tests for utils.image_paths.is_path_in_allowed_roots
    (pure logic — realpath + containment, no subprocess)."""

    def test_low7_allows_tmp_without_env_var(self):
        """When no project path is set, /tmp is allowed."""
        # Ensure the env var is not set
        os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)
        os.environ.pop("DEVELCAKES_ACTIVE_PROJECT_PATH", None)
        from utils.image_paths import is_path_in_allowed_roots
        assert is_path_in_allowed_roots("/tmp/somefile.png") is True

    def test_low7_rejects_etc_passwd_without_env_var(self):
        """When no project path is set, /etc/passwd is rejected."""
        os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)
        os.environ.pop("DEVELCAKES_ACTIVE_PROJECT_PATH", None)
        from utils.image_paths import is_path_in_allowed_roots
        assert is_path_in_allowed_roots("/etc/passwd") is False

    def test_low7_allows_project_path_when_set(self):
        """When CRABCAKES_ACTIVE_PROJECT_PATH is set, that path is allowed."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            file_path = os.path.join(tmp, "image.png")
            # Create the file so isfile passes in the (deleted) viewer path
            open(file_path, "w").close()
            from utils.image_paths import is_path_in_allowed_roots
            assert is_path_in_allowed_roots(file_path) is True
            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)

    def test_low7_rejects_path_outside_project(self):
        """A path outside the allowed roots is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            from utils.image_paths import is_path_in_allowed_roots
            assert is_path_in_allowed_roots("/etc/passwd") is False
            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)

    def test_low7_rejects_symlink_to_etc_passwd(self):
        """A symlink inside allowed root that points outside is rejected (realpath resolves it)."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"] = tmp
            from utils.image_paths import is_path_in_allowed_roots
            # Create a symlink inside tmp pointing to /etc/passwd
            symlink_path = os.path.join(tmp, "malicious.png")
            try:
                os.symlink("/etc/passwd", symlink_path)
            except OSError:
                pytest.skip("symlink creation not allowed on this system")
            # /etc/passwd is not in allowed roots, so the resolved path is rejected
            assert is_path_in_allowed_roots(symlink_path) is False
            os.environ.pop("CRABCAKES_ACTIVE_PROJECT_PATH", None)
