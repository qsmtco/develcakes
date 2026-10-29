# tests/test_diff_card.py
# Tests for ui/views/diff_card.py
#
# SPEC-58 §2c/§2i: the build_file_diff_card / build_diff_summary_card
# builders are deleted (the drawer imports only get_lang_from_path and
# render_diff_hunks); their test classes went with them.

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

import pathlib

from utils.diff_parser import DiffHunk, DiffLine
from ui.views.diff_card import (
    get_lang_from_path,
    render_diff_hunks,
)


class TestGetLangFromPath:
    """get_lang_from_path maps file extensions to language strings."""

    def test_python_extension(self):
        assert get_lang_from_path("foo.py") == "python"

    def test_javascript_extension(self):
        assert get_lang_from_path("foo.js") == "javascript"

    def test_typescript_extension(self):
        assert get_lang_from_path("foo.ts") == "typescript"

    def test_go_extension(self):
        assert get_lang_from_path("foo.go") == "go"

    def test_rust_extension(self):
        assert get_lang_from_path("foo.rs") == "rust"

    def test_markdown_extension(self):
        assert get_lang_from_path("README.md") == "markdown"

    def test_yaml_extension(self):
        assert get_lang_from_path("config.yml") == "yaml"

    def test_yaml_alt_extension(self):
        assert get_lang_from_path("config.yaml") == "yaml"

    def test_makefile_detection(self):
        assert get_lang_from_path("Makefile") == "makefile"

    def test_dockerfile_detection(self):
        assert get_lang_from_path("Dockerfile") == "dockerfile"

    def test_dockerfile_case_insensitive(self):
        assert get_lang_from_path("dockerfile") == "dockerfile"

    def test_unknown_extension(self):
        assert get_lang_from_path("foo.xyz") is None

    def test_uuid_no_extension(self):
        assert get_lang_from_path("LICENSE") is None

    def test_empty_string_returns_none(self):
        assert get_lang_from_path("") is None

    def test_non_string_input_returns_none(self):
        assert get_lang_from_path(None) is None  # type: ignore
        assert get_lang_from_path(42) is None  # type: ignore
        assert get_lang_from_path([]) is None  # type: ignore

    def test_dockerfile_prod_variant(self):
        """Dockerfile.prod, Dockerfile.dev, Dockerfile.test all resolve."""
        assert get_lang_from_path("Dockerfile.prod") == "dockerfile"
        assert get_lang_from_path("Dockerfile.dev") == "dockerfile"
        assert get_lang_from_path("Dockerfile.test") == "dockerfile"

    def test_dockerfile_variant_case_insensitive(self):
        """dockerfile.prod should also resolve."""
        assert get_lang_from_path("dockerfile.prod") == "dockerfile"

    def test_pathlib_path(self):
        """Accepts pathlib.Path objects."""
        assert get_lang_from_path(pathlib.Path("foo.py")) == "python"
        assert get_lang_from_path(pathlib.Path("Dockerfile")) == "dockerfile"
        assert get_lang_from_path(pathlib.Path("Dockerfile.prod")) == "dockerfile"

    def test_trailing_slash_before_basename(self):
        """Trailing slashes in path don't break basename detection."""
        assert get_lang_from_path("/some/dir/Dockerfile/") == "dockerfile"
        assert get_lang_from_path("Makefile/") == "makefile"
        assert get_lang_from_path("foo.py/") is None


class TestRenderDiffHunks:
    """render_diff_hunks renders hunks as a Gtk.Box."""

    def test_render_diff_hunks(self):
        """Renders hunks correctly, returns Gtk.Box."""
        hunk = DiffHunk(
            header="@@ -1,3 +1,4 @@",
            old_start=1,
            new_start=1,
            lines=[
                DiffLine(type="context", content=" unchanged", old_line_no=1, new_line_no=1),
                DiffLine(type="remove", content="-old line", old_line_no=2, new_line_no=None),
                DiffLine(type="add", content="+new line", old_line_no=None, new_line_no=2),
            ],
        )
        result = render_diff_hunks([hunk], lang="python")
        assert isinstance(result, Gtk.Box)
        # Should contain the hunk header and each line
        children = list(result)
        assert len(children) >= 1  # at least the hunk view

    def test_render_diff_hunks_empty(self):
        """Empty list returns empty Gtk.Box."""
        result = render_diff_hunks([], lang=None)
        assert isinstance(result, Gtk.Box)
        assert list(result) == []

    def test_render_diff_hunks_no_lang(self):
        """Works without language parameter."""
        hunk = DiffHunk(
            header="@@ -1 +1 @@",
            old_start=1,
            new_start=1,
            lines=[
                DiffLine(type="context", content=" hello", old_line_no=1, new_line_no=1),
            ],
        )
        result = render_diff_hunks([hunk])
        assert isinstance(result, Gtk.Box)
        assert len(list(result)) == 1

    def test_render_diff_hunks_multiple_hunks(self):
        """Multiple hunks are all rendered."""
        hunk1 = DiffHunk(
            header="@@ -1,2 +1,2 @@",
            old_start=1,
            new_start=1,
            lines=[DiffLine(type="context", content=" a", old_line_no=1, new_line_no=1)],
        )
        hunk2 = DiffHunk(
            header="@@ -5,2 +5,2 @@",
            old_start=5,
            new_start=5,
            lines=[DiffLine(type="context", content=" b", old_line_no=5, new_line_no=5)],
        )
        result = render_diff_hunks([hunk1, hunk2])
        assert isinstance(result, Gtk.Box)
        children = list(result)
        assert len(children) == 2


# ═══════════════════════════════════════════════════════════════════════
# Moved from tests/test_diff_viewer.py before its deletion (SPEC-58 §2i):
# get_lang_from_path still lives (ui/views/diff_card.py, file_tree.py).
# ═══════════════════════════════════════════════════════════════════════


class TestGetLangFromPathIntegration:
    """get_lang_from_path works with common extensions."""
    # Same mapping used by diff_card / file_tree internally

    def test_python(self):
        assert get_lang_from_path("foo.py") == "python"

    def test_javascript(self):
        assert get_lang_from_path("foo.js") == "javascript"

    def test_typescript(self):
        assert get_lang_from_path("foo.ts") == "typescript"

    def test_go(self):
        assert get_lang_from_path("foo.go") == "go"

    def test_rust(self):
        assert get_lang_from_path("foo.rs") == "rust"

    def test_markdown(self):
        assert get_lang_from_path("README.md") == "markdown"

    def test_yaml(self):
        assert get_lang_from_path("config.yml") == "yaml"

    def test_dockerfile(self):
        assert get_lang_from_path("Dockerfile") == "dockerfile"

    def test_dockerfile_variant(self):
        assert get_lang_from_path("Dockerfile.prod") == "dockerfile"

    def test_makefile(self):
        assert get_lang_from_path("Makefile") == "makefile"

    def test_pathlib_path(self):
        import pathlib
        assert get_lang_from_path(pathlib.Path("foo.py")) == "python"

    def test_empty_path(self):
        assert get_lang_from_path("") is None

    def test_unknown_extension(self):
        assert get_lang_from_path("foo.xyz") is None
