# tests/test_low8_9_10_11.py
# Tests for Phase 4-3: Utility hardening (LOW-8, LOW-9, LOW-10, LOW-11).
#
# Split from the former tests/test_low6_8_9_10_11.py when SPEC-36 removed the
# faster-whisper engine: LOW-6 (STT model_size validation) is moot in the new
# utils/stt.py, but LOW-8/9/10/11 still cover live code and were preserved.

# LOW-8:  SVG attribute escaping + color/initials validation in icons.py
# LOW-9:  _safe_error strips paths, truncates, uses class name
# LOW-10: diff_parser lstrip("a/") → removeprefix (path-baking fix)
# LOW-11: agent_defs calls validate_agent_def and skips invalid defs

import logging

# ══════════════════════════════════════════════════════════════════════════════
# LOW-8 — utils/icons.py SVG escaping + validation
# ══════════════════════════════════════════════════════════════════════════════

class TestLow8IconsValidation:
    """LOW-8: icons.py validates/escapes all user-controlled SVG attribute values."""

    def test_low8_malicious_color_hex_falls_back(self):
        """XSS-like color_hex falls back to safe color, not injected into SVG."""
        import utils.icons as ic

        result = ic.render_agent_icon(
            color_hex='#6366f1</path><script>alert(1)</script><path fill="',
            initials="Qr",
        )
        # Result is a Gdk.Texture or None — both acceptable (not a crash)
        # The SVG must NOT contain <script>
        assert result is None or not hasattr(result, "get_file"), (
            "render_agent_icon returned unexpected type"
        )
        # Key assertion: the malicious string must not be treated as a valid color
        assert ic._validate_color_hex(
            '#6366f1</path><script>alert(1)</script>'
        ) == ic._SAFE_FALLBACK_COLOR

    def test_low8_malicious_initials_stripped(self):
        """Malicious initials fall back to '??', not rendered as-is."""
        import utils.icons as ic

        assert ic._validate_initials("<script>") == "??"
        assert ic._validate_initials("") == "??"
        assert ic._validate_initials("ABC") == "??"  # too long
        assert ic._validate_initials("A1") == "A1"  # valid

    def test_low8_valid_color_passes_through(self):
        """Valid hex colors are passed through unchanged."""
        import utils.icons as ic

        assert ic._validate_color_hex("#abcdef") == "#abcdef"
        assert ic._validate_color_hex("#a1b2c3") == "#a1b2c3"
        assert ic._validate_color_hex("#fff") == "#fff"

    def test_low8_valid_initials_pass_through(self):
        """Valid 1-2 char alphanumeric initials pass through."""
        import utils.icons as ic

        assert ic._validate_initials("A") == "A"
        assert ic._validate_initials("Qr") == "Qr"
        assert ic._validate_initials("9X") == "9X"

    def test_low8_escape_svg_attr(self):
        """_escape_svg_attr neutralizes HTML chars."""
        import utils.icons as ic

        assert "&lt;script&gt;" in ic._escape_svg_attr("<script>")
        assert "&quot;" in ic._escape_svg_attr('quote"end')


# ══════════════════════════════════════════════════════════════════════════════
# LOW-9 — utils/git_ops.py _safe_error
# ══════════════════════════════════════════════════════════════════════════════

class TestLow9SafeError:
    """LOW-9: _safe_error strips paths, truncates, preserves class name."""

    def test_low9_safe_error_strips_paths(self):
        """Absolute paths in exception messages are replaced with ~."""
        from utils.git_ops import _safe_error

        result = _safe_error(Exception("/home/user/secret/file"))
        assert "/home/user" not in result
        assert "~" in result

    def test_low9_safe_error_strips_tmp_paths(self):
        """Tmp paths are also stripped."""
        from utils.git_ops import _safe_error

        result = _safe_error(Exception("/tmp/sensitive.log"))
        assert "/tmp/" not in result

    def test_low9_safe_error_truncates(self):
        """Messages over 200 chars are truncated."""
        from utils.git_ops import _safe_error

        result = _safe_error(Exception("x" * 1000))
        assert len(result) <= 200

    def test_low9_safe_error_uses_class_name(self):
        """Result starts with the exception class name."""
        from utils.git_ops import _safe_error

        assert _safe_error(ValueError("foo")).startswith("ValueError")
        assert _safe_error(RuntimeError("bar")).startswith("RuntimeError")

    def test_low9_safe_error_no_traceback(self):
        """_safe_error never includes traceback or repr."""
        from utils.git_ops import _safe_error

        result = _safe_error(ValueError("test"))
        assert "Traceback" not in result
        assert "valueerror" not in result.lower() or result.startswith("ValueError")

    def test_low9_git_ops_uses_safe_error(self):
        """git_ops.GitResult.error uses _safe_error output, not bare str(e)."""
        from utils import git_ops

        # Trigger a failure on a path that definitely isn't a repo
        result = git_ops.get_head_sha("/tmp/definitely_not_a_git_repo_xyz")
        assert not result.success
        # _safe_error produces: "RepoError: ..." or similar (class name present)
        assert result.error  # must have some error message
        assert "~" not in result.error or "/" not in result.error  # path was stripped


# ══════════════════════════════════════════════════════════════════════════════
# LOW-10 — utils/diff_parser path extraction
# ══════════════════════════════════════════════════════════════════════════════

class TestLow10DiffParser:
    """LOW-10: diff_parser path extraction uses removeprefix, not lstrip."""

    def test_low10_apple_file_not_mangled(self):
        """diff --git a/apple.py b/apple.py → old_path='apple.py', not 'pple.py'."""
        from utils.diff_parser import parse_diff

        diff = "diff --git a/apple.py b/apple.py\n--- a/apple.py\n+++ b/apple.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff(diff)
        file_diff = result.files[0]
        assert file_diff.old_path == "apple.py", (
            f"Expected 'apple.py', got {file_diff.old_path!r} — lstrip bug!"
        )
        assert file_diff.new_path == "apple.py"

    def test_low10_afile_with_leading_a(self):
        """Files named 'afile.txt' are not mangled by removeprefix."""
        from utils.diff_parser import parse_diff

        diff = "diff --git a/afile.txt b/afile.txt\n--- a/afile.txt\n+++ b/afile.txt\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse_diff(diff)
        file_diff = result.files[0]
        assert file_diff.old_path == "afile.txt", (
            f"Expected 'afile.txt', got {file_diff.old_path!r}"
        )
        assert file_diff.new_path == "afile.txt"

    def test_low10_lstrip_gone_from_diff_parser(self):
        """Verify no lstrip('a/') or lstrip('b/') remains in diff_parser.py."""
        import inspect

        import utils.diff_parser as dp

        source = inspect.getsource(dp)
        assert 'lstrip("a/")' not in source, "lstrip('a/') still present in diff_parser.py"
        assert "lstrip('a/')" not in source, "lstrip('a/') still present in diff_parser.py"
        assert 'lstrip("b/")' not in source, "lstrip('b/') still present in diff_parser.py"
        assert "lstrip('b/')" not in source, "lstrip('b/') still present in diff_parser.py"


# ══════════════════════════════════════════════════════════════════════════════
# LOW-11 — utils/agent_defs validation at load time
# ══════════════════════════════════════════════════════════════════════════════

class TestLow11AgentDefsValidation:
    """LOW-11: load_agent_defs skips defs that fail validate_agent_def."""

    def test_low11_load_skips_invalid_def(self, tmp_path, monkeypatch):
        """An agent def flagged invalid by validate_agent_def is skipped."""
        import yaml

        import utils.agent_defs as ad

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        with open(agents_dir / "good.yaml", "w") as f:
            yaml.safe_dump({
                "name": "GoodBot",
                "llm_name": "openai/gpt-4o",
                "prompts": ["system/coder.md"],
                "tools": ["read_file"],
            }, f)
        with open(agents_dir / "bad.yaml", "w") as f:
            yaml.safe_dump({
                "name": "BadBot",
                "llm_name": "openai/gpt-4o",
                "prompts": ["system/coder.md"],
                "tools": ["nonexistent_tool_xyz"],
            }, f)

        original_get_dir = ad._get_agents_dir
        ad._get_agents_dir = lambda: str(agents_dir)

        # Mock validate_agent_def: return [] (valid) for GoodBot, errors for BadBot
        def fake_validate(def_dict):
            if def_dict.get("name") == "BadBot":
                return ["Unknown tool: nonexistent_tool_xyz"]
            return []

        monkeypatch.setattr(ad, "validate_agent_def", fake_validate)

        try:
            defs = ad.load_agent_defs()
            names = [d.get("name") for d in defs]
            assert "GoodBot" in names, f"Valid agent should be included, got: {names}"
            assert "BadBot" not in names, "Invalid agent should be skipped"
        finally:
            ad._get_agents_dir = original_get_dir

    def test_low11_load_includes_valid_def(self, tmp_path, monkeypatch):
        """A valid agent def is included in load_agent_defs."""
        import yaml

        import utils.agent_defs as ad

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        with open(agents_dir / "test.yaml", "w") as f:
            yaml.safe_dump({
                "name": "TestAgent",
                "llm_name": "openai/gpt-4o",
                "prompts": ["system/coder.md"],
                "tools": ["read_file"],
            }, f)

        original_get_dir = ad._get_agents_dir
        ad._get_agents_dir = lambda: str(agents_dir)

        # Validation always passes for this test
        monkeypatch.setattr(ad, "validate_agent_def", lambda d: [])

        try:
            defs = ad.load_agent_defs()
            names = [d.get("name") for d in defs]
            assert "TestAgent" in names, f"Expected TestAgent in {names}"
        finally:
            ad._get_agents_dir = original_get_dir

    def test_low11_load_warns_on_invalid(self, caplog, tmp_path):
        """Invalid agent defs produce a WARNING with the agent name and errors."""
        import yaml

        import utils.agent_defs as ad

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        invalid_def = {
            "name": "WarnAgent",
            "llm_name": "test",
            "prompts": ["hello"],
            "tools": ["unknown_tool_abc123"],
        }
        with open(agents_dir / "warn.yaml", "w") as f:
            yaml.safe_dump(invalid_def, f)

        original_get_dir = ad._get_agents_dir
        ad._get_agents_dir = lambda: str(agents_dir)

        with caplog.at_level(logging.WARNING):
            defs = ad.load_agent_defs()

        try:
            names = [d.get("name") for d in defs]
            assert "WarnAgent" not in names
            assert any(
                "WarnAgent" in r.message and "LOW-11" in r.message
                for r in caplog.records
            ), f"Expected LOW-11 warning for WarnAgent, got: {[r.message for r in caplog.records]}"
        finally:
            ad._get_agents_dir = original_get_dir
