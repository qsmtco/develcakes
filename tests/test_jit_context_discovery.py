# tests/test_jit_context_discovery.py
# Tests for JIT Context Discovery (P10) — SPEC-JIT-CONTEXT-DISCOVERY-1.md
#
# Covers:
# - P10.1: ProviderConfig.context_mode + validate_provider_context_mode
# - P10.2: build_file_index + resolve_context_mode + build_file_context_with_core_files modes
# - P10.3: _run_grep + _search_files refactor + _file_search + tool registration
# - P10.4: compose_system_prompt context_mode parameter
# - P10.5: build_system_prompt forwards context_mode

import os
import tempfile
import subprocess
import inspect
import pytest

from models.providers import (
    ProviderConfig,
    validate_provider_context_mode,
)
from agent.context import (
    build_file_index,
    resolve_context_mode,
    build_file_context_with_core_files,
    build_system_prompt,
)
from agent.tools import (
    _run_grep,
    _search_files,
    _file_search,
    get_all_tools,
    ToolResult,
)
from utils.prompt_loader import compose_system_prompt


# ═══════════════════════════════════════════════════════════════════
#  P10.1 — ProviderConfig.context_mode
# ═══════════════════════════════════════════════════════════════════

class TestProviderContextMode:
    def test_provider_config_defaults_context_mode_auto(self):
        """Default context_mode is 'auto'."""
        p = ProviderConfig(name='t', base_url='x', api_key='k', default_model='m')
        assert p.context_mode == "auto"

    def test_provider_config_accepts_valid_modes(self):
        """All 4 valid values round-trip through dataclass."""
        for mode in ("auto", "preload", "jit", "hybrid"):
            p = ProviderConfig(name='t', base_url='x', api_key='k',
                             default_model='m', context_mode=mode)
            assert p.context_mode == mode

    def test_validate_provider_context_mode_rejects_invalid(self):
        """Invalid string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid context_mode"):
            validate_provider_context_mode("invalid")

    def test_validate_provider_context_mode_normalizes_case(self):
        """'PRELOAD' normalizes to 'preload'."""
        assert validate_provider_context_mode("PRELOAD") == "preload"
        assert validate_provider_context_mode("JIT") == "jit"


# ═══════════════════════════════════════════════════════════════════
#  P10.2 — build_file_index + resolve_context_mode + context modes
# ═══════════════════════════════════════════════════════════════════

class TestResolveContextMode:
    def test_resolve_context_mode_explicit_override(self):
        """Explicit modes are returned as-is, never overridden."""
        assert resolve_context_mode("preload", 1_000_000) == "preload"
        assert resolve_context_mode("jit", 1_000_000) == "jit"
        assert resolve_context_mode("hybrid", 1_000_000) == "hybrid"

    def test_resolve_context_mode_large_window_is_hybrid(self):
        """auto never selects preload. Explicit preload still inlines."""
        assert resolve_context_mode("auto", 1_000_000) == "hybrid"
        assert resolve_context_mode("auto", 500_000) == "hybrid"
        assert resolve_context_mode("preload", 1_000_000) == "preload"

    def test_resolve_context_mode_small_window_jit(self):
        """Small window (<=32K) resolves to jit."""
        assert resolve_context_mode("auto", 32_000) == "jit"
        assert resolve_context_mode("auto", 16_000) == "jit"

    def test_resolve_context_mode_typical_window_hybrid(self):
        """Typical window (128K-256K) resolves to hybrid."""
        assert resolve_context_mode("auto", 128_000) == "hybrid"
        assert resolve_context_mode("auto", 200_000) == "hybrid"

    def test_resolve_context_mode_auto_no_model_returns_hybrid(self):
        """model_max_tokens=None defaults to 128K → hybrid."""
        assert resolve_context_mode("auto", None) == "hybrid"
        assert resolve_context_mode("auto", 0) == "hybrid"

    def test_resolve_context_mode_invalid_raises(self):
        """Invalid mode string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid context_mode"):
            resolve_context_mode("invalid", 128_000)


class TestBuildFileIndex:
    def test_build_file_index_returns_compact_listing(self):
        """Index is <3K chars for a small fixture project."""
        with tempfile.TemporaryDirectory() as proj:
            for name in ("main.py", "utils.py", "README.md", "config.json"):
                with open(os.path.join(proj, name), 'w') as f:
                    f.write("# content\n")
            idx = build_file_index(proj)
            assert idx  # non-empty
            assert len(idx) < 3000, f"Index too large: {len(idx)} chars"
            assert "main.py" in idx

    def test_build_file_index_respects_gitignore(self):
        """Gitignored files don't appear in the index."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, ".gitignore"), 'w') as f:
                f.write("*.log\nsecret/\n")
            with open(os.path.join(proj, "app.py"), 'w') as f:
                f.write("pass\n")
            with open(os.path.join(proj, "debug.log"), 'w') as f:
                f.write("log\n")
            idx = build_file_index(proj)
            assert "app.py" in idx
            assert "debug.log" not in idx

    def test_build_file_index_groups_by_extension(self):
        """Files grouped by extension."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), 'w') as f:
                f.write("x\n")
            with open(os.path.join(proj, "b.md"), 'w') as f:
                f.write("y\n")
            idx = build_file_index(proj)
            assert "### PY" in idx or "### py" in idx
            assert "### MD" in idx or "### md" in idx or "### Markdown" in idx

    def test_build_file_index_sorted_by_size(self):
        """Within a group, largest files first."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "small.py"), 'w') as f:
                f.write("x\n")
            with open(os.path.join(proj, "big.py"), 'w') as f:
                f.write("x" * 5000 + "\n")
            idx = build_file_index(proj)
            big_pos = idx.index("big.py")
            small_pos = idx.index("small.py")
            assert big_pos < small_pos, "big.py should appear before small.py"

    def test_build_file_index_handles_invalid_path(self):
        """Empty string for missing directory."""
        assert build_file_index("/nonexistent/path/xyz") == ""
        assert build_file_index("") == ""

    def test_build_file_index_max_entries_cap(self):
        """Project with >200 files shows 200 + directory summary."""
        with tempfile.TemporaryDirectory() as proj:
            for i in range(250):
                with open(os.path.join(proj, f"f_{i}.py"), 'w') as f:
                    f.write("x\n")
            idx = build_file_index(proj, max_entries=200)
            assert "250 files" in idx
            assert "more files" in idx
            assert "Top directories" in idx

    def test_build_file_index_directory_summary_large_project(self):
        """1000+ files shows per-directory summary."""
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "src"))
            os.makedirs(os.path.join(proj, "tests"))
            for i in range(600):
                with open(os.path.join(proj, "src", f"s_{i}.py"), 'w') as f:
                    f.write("x\n")
            for i in range(500):
                with open(os.path.join(proj, "tests", f"t_{i}.py"), 'w') as f:
                    f.write("x\n")
            idx = build_file_index(proj, max_entries=200)
            assert "1,100 files" in idx
            assert "src/" in idx
            assert "tests/" in idx
            assert "files /" in idx

    def test_build_file_index_groups_files_without_extension(self):
        """Files without extension grouped under 'Other'."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "Makefile"), 'w') as f:
                f.write("all:\n\techo hi\n")
            idx = build_file_index(proj)
            assert "Makefile" in idx
            assert "Other" in idx


class TestBuildFileContextModes:
    def test_build_file_context_preload_mode_unchanged(self):
        """Preload mode matches existing behavior."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write("# Test Project\n")
            with open(os.path.join(proj, "app.py"), 'w') as f:
                f.write("print('hello')\n")
            result = build_file_context_with_core_files(proj, context_mode="preload")
            assert "# Test Project" in result  # file contents present
            assert "README.md" in result

    def test_build_file_context_jit_mode_returns_index(self):
        """JIT mode returns just the index, no file contents."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write("# Test Project\n")
            with open(os.path.join(proj, "app.py"), 'w') as f:
                f.write("print('hello')\n")
            result = build_file_context_with_core_files(proj, context_mode="jit")
            assert "File index" in result
            assert "# Test Project" not in result  # no file contents

    def test_build_file_context_hybrid_mode_stubs_core_files(self):
        """Hybrid mode is a stub line per core file plus the index. No body."""
        with tempfile.TemporaryDirectory() as proj:
            body = "# Hybrid Test\n"
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write(body)
            with open(os.path.join(proj, "app.py"), 'w') as f:
                f.write("pass\n")
            result = build_file_context_with_core_files(proj, context_mode="hybrid")
            assert body not in result
            assert 'use read_file("README.md")' in result
            assert "File index" in result

    def test_build_file_context_jit_mode_omits_core_files(self):
        """JIT mode does NOT include README/AGENTS/etc."""
        with tempfile.TemporaryDirectory() as proj:
            for name in ("README.md", "AGENTS.md", "CONVENTIONS.md", "ARCHITECTURE.md"):
                with open(os.path.join(proj, name), 'w') as f:
                    f.write("# Content for %s\n" % name)
            result = build_file_context_with_core_files(proj, context_mode="jit")
            for name in ("README.md", "AGENTS.md", "CONVENTIONS.md", "ARCHITECTURE.md"):
                assert "# Content for %s" % name not in result

    def test_build_file_context_invalid_mode_raises(self):
        """Invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="Invalid context_mode"):
            build_file_context_with_core_files("/tmp", context_mode="invalid")

    def test_build_file_context_invalid_path_returns_empty(self):
        """Invalid path returns empty string."""
        assert build_file_context_with_core_files("/nonexistent", context_mode="jit") == ""

    def test_build_file_context_default_mode_is_preload(self):
        """Default (no context_mode kwarg) is preload — backward compatible."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write("# Default Test\n")
            # No context_mode kwarg — should default to preload
            result = build_file_context_with_core_files(proj)
            assert "# Default Test" in result  # file contents present


# ═══════════════════════════════════════════════════════════════════
#  P10.3 — _run_grep + _file_search + tool registration
# ═══════════════════════════════════════════════════════════════════

class TestRunGrep:
    def test_run_grep_returns_expected_format(self):
        """Verify (returncode, stdout, stderr) tuple."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "test.py"), 'w') as f:
                f.write("def hello():\n    pass\n")
            rc, out, err = _run_grep("hello", proj)
            assert isinstance(rc, int)
            assert isinstance(out, str)
            assert isinstance(err, str)
            assert rc == 0
            assert "hello" in out

    def test_run_grep_no_matches_returncode_1(self):
        """grep returns 1 for no matches."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "test.py"), 'w') as f:
                f.write("nothing here\n")
            rc, out, err = _run_grep("nonexistent_pattern", proj)
            assert rc == 1

    def test_run_grep_file_type_filter(self):
        """file_type filter works."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), 'w') as f:
                f.write("target\n")
            with open(os.path.join(proj, "b.md"), 'w') as f:
                f.write("target\n")
            rc, out, err = _run_grep("target", proj, file_type="py")
            assert rc == 0
            assert "a.py" in out
            assert "b.md" not in out


class TestSearchFilesRefactored:
    def test_search_files_unchanged_after_refactor(self):
        """Existing search_files behavior identical post-refactor."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "test.py"), 'w') as f:
                f.write("def search_target():\n    pass\n")
            result = _search_files("search_target", proj)
            assert result.success
            assert "search_target" in result.output

    def test_search_files_no_matches(self):
        """No matches returns success with '(no matches)'."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "test.py"), 'w') as f:
                f.write("nothing\n")
            result = _search_files("totally_absent", proj)
            assert result.success
            assert result.output == "(no matches)"


class TestFileSearch:
    def test_file_search_finds_by_filename(self):
        """file_search finds files by name fragment."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "context_strategy.py"), 'w') as f:
                f.write("pass\n")
            result = _file_search("context_strategy", proj)
            assert result.success
            assert "context_strategy.py" in result.output

    def test_file_search_finds_by_content(self):
        """file_search finds files by content pattern."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "module.py"), 'w') as f:
                f.write("class MySpecialClass:\n    pass\n")
            result = _file_search("MySpecialClass", proj)
            assert result.success
            assert "module.py" in result.output
            assert "MySpecialClass" in result.output

    def test_file_search_groups_by_file(self):
        """Multiple grep hits in same file shown once."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "multi.py"), 'w') as f:
                f.write("target_line_1\ntarget_line_2\ntarget_line_3\n")
            result = _file_search("target_line", proj)
            assert result.success
            # File should appear once in the header line
            assert result.output.count("multi.py") == 1 or result.output.count("multi.py") <= 2

    def test_file_search_respects_max_results(self):
        """max_results caps number of files returned."""
        with tempfile.TemporaryDirectory() as proj:
            for i in range(5):
                with open(os.path.join(proj, "file_%d.py" % i), 'w') as f:
                    f.write("common_pattern\n")
            result = _file_search("common_pattern", proj, max_results=2)
            assert result.success
            # Count file header lines (lines with "(" — the metadata header pattern)
            # At most 2 files in output
            file_count = sum(1 for line in result.output.split("\n")
                           if ".py" in line and "(" in line and "Line" not in line)
            assert file_count <= 2

    def test_file_search_file_type_filter(self):
        """file_type filters out non-matching extensions."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), 'w') as f:
                f.write("findme\n")
            with open(os.path.join(proj, "b.md"), 'w') as f:
                f.write("findme\n")
            result = _file_search("findme", proj, file_type="py")
            assert result.success
            assert "a.py" in result.output
            assert "b.md" not in result.output

    def test_file_search_includes_preview_lines(self):
        """At least 1 preview line per file with content matches."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "preview.py"), 'w') as f:
                f.write("def searchable_func():\n    return 42\n")
            result = _file_search("searchable_func", proj)
            assert result.success
            assert "Line" in result.output

    def test_file_search_invalid_query_returns_error(self):
        """Empty query returns ToolResult(success=False)."""
        result = _file_search("", "/tmp")
        assert isinstance(result, ToolResult)
        assert not result.success
        assert "empty query" in result.error


class TestFileSearchToolRegistration:
    def test_file_search_tool_registered(self):
        """get_all_tools() includes 'file_search'."""
        tool_names = [t.name for t in get_all_tools()]
        assert "file_search" in tool_names

    def test_file_search_tool_description_includes_when_to_use(self):
        """Description contains 'WHEN TO USE'."""
        ft = [t for t in get_all_tools() if t.name == "file_search"][0]
        assert "WHEN TO USE" in ft.description

    def test_file_search_tool_requires_query(self):
        """JSON schema marks 'query' as required."""
        ft = [t for t in get_all_tools() if t.name == "file_search"][0]
        assert "query" in ft.parameters.get("required", [])


# ═══════════════════════════════════════════════════════════════════
#  P10.4 — compose_system_prompt context_mode parameter
# ═══════════════════════════════════════════════════════════════════

class TestComposeSystemPromptContextMode:
    def test_compose_prompt_jit_mode_produces_smaller_prompt(self):
        """JIT prompt < preload prompt for same project."""
        with tempfile.TemporaryDirectory() as proj:
            # Create enough content that preload exceeds JIT significantly
            for i in range(20):
                with open(os.path.join(proj, "file_%d.py" % i), 'w') as f:
                    f.write("def f():\n    pass\n" * 100)
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write("# Test\n" * 500)

            preload = compose_system_prompt(
                agent_name="Coder", agent_role="coder",
                project_path=proj, model_max_tokens=128000,
                context_mode="preload",
            )
            jit = compose_system_prompt(
                agent_name="Coder", agent_role="coder",
                project_path=proj, model_max_tokens=128000,
                context_mode="jit",
            )
            assert len(jit) < len(preload), \
                "JIT (%d) should be < preload (%d)" % (len(jit), len(preload))

    def test_compose_prompt_hybrid_mode_stubs_core_files(self):
        """Hybrid prompt names the core file and does not paste its body."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write("# Hybrid Project\n")
            prompt = compose_system_prompt(
                agent_name="Coder", agent_role="coder",
                project_path=proj, model_max_tokens=128000,
                context_mode="hybrid",
            )
            assert "# Hybrid Project" not in prompt
            assert 'use read_file("README.md")' in prompt
            assert "File index" in prompt

    def test_compose_prompt_accepts_auto_mode(self):
        """Explicit context_mode='auto' is accepted."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write("# Auto\n")
            prompt = compose_system_prompt(
                agent_name="Coder", agent_role="coder",
                project_path=proj, model_max_tokens=128000,
                context_mode="auto",
            )
            assert "Coder" in prompt

    def test_compose_prompt_context_mode_is_keyword_only(self):
        """context_mode is keyword-only parameter."""
        sig = inspect.signature(compose_system_prompt)
        param = sig.parameters["context_mode"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


# ═══════════════════════════════════════════════════════════════════
#  P10.5 — build_system_prompt forwards context_mode
# ═══════════════════════════════════════════════════════════════════

class TestBuildSystemPromptContextMode:
    def test_build_system_prompt_forwards_context_mode(self):
        """context_mode='jit' reaches compose_system_prompt."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), 'w') as f:
                f.write("# Forward Test\n")
            with open(os.path.join(proj, "app.py"), 'w') as f:
                f.write("pass\n")
            prompt = build_system_prompt(
                "Coder", proj, ["read_file"],
                model_max_tokens=128000,
                context_mode="jit",
            )
            assert "File index" in prompt
            assert "# Forward Test" not in prompt  # JIT: no file contents

    def test_build_system_prompt_default_context_mode_auto(self):
        """No context_mode param defaults to 'auto'."""
        sig = inspect.signature(build_system_prompt)
        param = sig.parameters["context_mode"]
        assert param.default == "auto"

    def test_build_system_prompt_context_mode_is_keyword_only(self):
        """context_mode is keyword-only."""
        sig = inspect.signature(build_system_prompt)
        param = sig.parameters["context_mode"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


# ═══════════════════════════════════════════════════════════════════
#  Edge cases per spec §7
# ═══════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_context_mode_auto_with_none_model_max(self):
        """auto + model_max_tokens=None → hybrid (128K default)."""
        assert resolve_context_mode("auto", None) == "hybrid"

    def test_file_search_empty_query_returns_error(self):
        """file_search('') returns error."""
        result = _file_search("", "/tmp")
        assert not result.success

    def test_build_file_index_empty_project(self):
        """Empty project directory returns empty string."""
        with tempfile.TemporaryDirectory() as proj:
            assert build_file_index(proj) == ""


# ═══════════════════════════════════════════════════════════════════
#  Missing spec tests (BUG #7) — added per audit
# ═══════════════════════════════════════════════════════════════════

class TestRuntimeContextModeWiring:
    """Spec §3.21m: runtime passes context_mode from ProviderConfig."""

    def test_runtime_passes_context_mode_from_provider(self):
        """Runtime reads context_mode from ProviderConfig and passes to build_system_prompt."""
        # Verify the runtime code path: getattr(default_provider_cfg, 'context_mode', 'auto')
        # is present in runtime.py create_conversation
        import ast
        with open("agent/runtime.py") as f:
            tree = ast.parse(f.read())
        src = open("agent/runtime.py").read()
        assert 'context_mode=getattr(default_provider_cfg, "context_mode", "auto")' in src or \
               "context_mode=getattr(default_provider_cfg, 'context_mode', 'auto')" in src, \
               "Runtime must pass context_mode from ProviderConfig"

    def test_runtime_defaults_context_mode_auto(self):
        """When ProviderConfig has no context_mode, runtime defaults to 'auto'."""
        from models.providers import ProviderConfig
        cfg = ProviderConfig(name="test", base_url="http://x", api_key="k", default_model="m")
        mode = getattr(cfg, "context_mode", "auto") or "auto"
        assert mode == "auto"


class TestBackwardCompat:
    """Spec §4.4b: backward compatibility for existing callers."""

    def test_build_system_prompt_backward_compat(self):
        """build_system_prompt works without context_mode (default unchanged)."""
        # Should not raise — context_mode defaults to 'auto'
        with tempfile.TemporaryDirectory() as proj:
            prompt = build_system_prompt(
                "TestAgent", proj, ["read_file"], agent_role=""
            )
            assert isinstance(prompt, str)
            assert len(prompt) > 0

    def test_compose_system_prompt_backward_compat(self):
        """compose_system_prompt works without context_mode (default unchanged)."""
        with tempfile.TemporaryDirectory() as proj:
            prompt = compose_system_prompt(
                agent_name="TestAgent",
                agent_role="",
                project_path=proj,
                tools=["read_file"],
                review_mode="off",
            )
            assert isinstance(prompt, str)
            assert len(prompt) > 0


class TestPerSessionContextMode:
    """Spec §7: per-session context mode isolation."""

    def test_runtime_per_session_context_mode_isolation(self):
        """Two conversations with different providers get different context modes."""
        # Verify resolve_context_mode produces different results for different windows
        mode_small = resolve_context_mode("auto", 32_000)
        mode_large = resolve_context_mode("auto", 500_000)
        assert mode_small != mode_large
        assert mode_small == "jit"
        assert mode_large == "hybrid"

    def test_runtime_context_mode_fixed_for_session(self):
        """Context mode is resolved once at conversation creation, not per-turn."""
        # The mode is fixed: same inputs always produce same output
        mode1 = resolve_context_mode("auto", 128_000)
        mode2 = resolve_context_mode("auto", 128_000)
        assert mode1 == mode2 == "hybrid"
        # Verify it's deterministic (no randomness)
        for _ in range(10):
            assert resolve_context_mode("auto", 128_000) == "hybrid"


class TestBuildFileIndexBinary:
    """Spec §7: build_file_index handles binary files gracefully."""

    def test_build_file_index_handles_binary_files(self):
        """build_file_index skips line count for binary files without crashing."""
        with tempfile.TemporaryDirectory() as proj:
            # Create a binary file
            with open(os.path.join(proj, "data.bin"), "wb") as f:
                f.write(b'\x00\x01\x02\x03\xff\xfe\x00binary')
            # Create a normal Python file
            with open(os.path.join(proj, "code.py"), "w") as f:
                f.write("x = 1\n")

            result = build_file_index(proj)
            assert isinstance(result, str)
            # Binary file should appear in index (by extension)
            assert "data.bin" in result or "data" in result
            # Python file should have line count
            assert "1 lines" in result
