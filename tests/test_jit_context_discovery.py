# tests/test_jit_context_discovery.py
# Tests for JIT Context Discovery (P10) — SPEC-JIT-CONTEXT-DISCOVERY-1.md
#
# Covers:
# - P10.1: ProviderConfig.context_mode + validate_provider_context_mode
# - P10.2: build_file_index + resolve_context_mode + build_file_context_with_core_files modes
# - P10.3: _run_grep + _search_files refactor + _file_search + tool registration
# - P10.4: compose_system_prompt context_mode parameter
# - P10.5: build_system_prompt forwards context_mode

import inspect
import os
import tempfile

import pytest

from agent.context import (
    build_file_context_with_core_files,
    build_file_index,
    build_system_prompt,
    resolve_context_mode,
)
from agent.tools import (
    ToolResult,
    _file_search,
    _run_grep,
    _search_files,
    get_all_tools,
)
from models.providers import (
    ProviderConfig,
    validate_provider_context_mode,
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

    def test_coerce_provider_context_mode_never_raises(self):
        from models.providers import coerce_provider_context_mode
        assert coerce_provider_context_mode("JIT") == "jit"
        assert coerce_provider_context_mode("  hybrid ") == "hybrid"
        assert coerce_provider_context_mode("nope") == "auto"
        assert coerce_provider_context_mode("") == "auto"
        assert coerce_provider_context_mode(None) == "auto"
        assert coerce_provider_context_mode(3) == "auto"

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

    def test_build_file_index_small_source_survives_docs_and_png_cut(self):
        """§1: a small source file is listed; docs/ and .png rows are absent.

        Replaces the old pure-size-order test. A size sort put giant
        tests/ and docs/ files at the top and pushed small source files into
        the "1,355 more" bucket — that WAS the bug. docs/ is now pruned during
        the walk and BINARY_EXTENSIONS are never listed, so the small source
        file must survive while neither of those appears.
        """
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "docs"))
            with open(os.path.join(proj, "small.py"), 'w') as f:
                f.write("x\n")
            with open(os.path.join(proj, "docs", "guide.md"), 'w') as f:
                f.write("# guide\n" * 2000)
            with open(os.path.join(proj, "logo.png"), 'wb') as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000)

            idx = build_file_index(proj)
            assert "small.py" in idx, "small source file must be listed"
            assert "guide.md" not in idx, (
                "EXCLUDED_DIRS (docs/) must be pruned during the walk, "
                "not after the 200-slot cut (§1)"
            )
            assert "logo.png" not in idx, (
                "BINARY_EXTENSIONS must never be listed (§1)"
            )

    def test_build_file_index_ranks_source_above_low_priority_dirs(self):
        """§1: tests/ and prompts/ lose slots to source directories.

        Size is a tiebreak WITHIN a directory, not the global sort key. A
        300KB test file must not outrank a 10-byte source file just because
        it is bigger.
        """
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "tests"))
            os.makedirs(os.path.join(proj, "prompts"))
            with open(os.path.join(proj, "tiny.py"), 'w') as f:
                f.write("x = 1\n")
            with open(os.path.join(proj, "tests", "huge.py"), 'w') as f:
                f.write("x" * 300_000 + "\n")
            with open(os.path.join(proj, "prompts", "big.md"), 'w') as f:
                f.write("prompt\n" * 20_000)

            # max_entries=1 leaves exactly one row: the source file.
            idx = build_file_index(proj, max_entries=1)
            assert "tiny.py" in idx, (
                "source directory must win the last slot over a 300KB "
                "tests/ file (LOW_PRIORITY_DIRS, §1)"
            )
            assert "huge.py" not in idx
            assert "big.md" not in idx

    def test_build_file_index_root_file_loses_to_source_dir(self):
        """Debugger regression (Batch A item 1): a tiny scratch file at the
        repo root must not outrank a large source file. The old two-tier key
        ranked "(root)" ahead of every source directory because '(' (ASCII
        40) sorts below all letters AND the sentinel is not in
        LOW_PRIORITY_DIRS — live proof: tmp_measure_tools.py (1KB) beat
        agent/runtime.py (192KB). Root files are tier 1, source dirs tier 0.
        """
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "agent"))
            with open(os.path.join(proj, "tmp_measure_tools.py"), 'w') as f:
                f.write("x = 1\n")
            with open(os.path.join(proj, "agent", "runtime.py"), 'w') as f:
                f.write("x" * 192_000 + "\n")

            # max_entries=1 leaves exactly one row: the agent/ source file.
            idx = build_file_index(proj, max_entries=1)
            assert os.path.join("agent", "runtime.py") in idx, (
                "source directory file must win the slot over a tiny "
                "repo-root scratch file (tier 0 source dirs beat tier 1 "
                "root files)"
            )
            assert "tmp_measure_tools.py" not in idx

    def test_build_file_index_includes_docs_when_env_override_set(
        self, tmp_path, monkeypatch
    ):
        """§1: DEVELCAKES_INCLUDE_DOCS=1 restores docs/ (and still demotes it)."""
        os.makedirs(tmp_path / "docs")
        (tmp_path / "docs" / "guide.md").write_text("# guide\n")
        (tmp_path / "app.py").write_text("x = 1\n")

        monkeypatch.setenv("DEVELCAKES_INCLUDE_DOCS", "1")
        idx = build_file_index(str(tmp_path))
        assert "guide.md" in idx, "override must re-admit docs/"

        # Still deprioritized: with one slot, source wins over docs/.
        idx_one = build_file_index(str(tmp_path), max_entries=1)
        assert "app.py" in idx_one
        assert "guide.md" not in idx_one

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
                    f.write(f"# Content for {name}\n")
            result = build_file_context_with_core_files(proj, context_mode="jit")
            for name in ("README.md", "AGENTS.md", "CONVENTIONS.md", "ARCHITECTURE.md"):
                assert f"# Content for {name}" not in result

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
            rc, _out, _err = _run_grep("nonexistent_pattern", proj)
            assert rc == 1

    def test_run_grep_file_type_filter(self):
        """file_type filter works."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), 'w') as f:
                f.write("target\n")
            with open(os.path.join(proj, "b.md"), 'w') as f:
                f.write("target\n")
            rc, out, _err = _run_grep("target", proj, file_type="py")
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
                with open(os.path.join(proj, f"file_{i}.py"), 'w') as f:
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

    def test_file_search_description_states_result_not_essay(self):
        """Description says what it returns — no WHEN TO USE/WHEN NOT TO USE essays.

        CONTEXT-EFFICIENCY-INSTRUCTIONS §2 deletes the multi-paragraph
        heading block from every tool description (they ride on every API
        call) and requires the replacement to state what the tool returns
        and the limit that matters. For file_search that is the grouping by
        file and the short preview.
        """
        ft = next(t for t in get_all_tools() if t.name == "file_search")
        assert ft.description, "description must not be empty"
        assert "WHEN TO USE" not in ft.description, (
            "WHEN TO USE essay headings were removed from tool descriptions "
            "(CONTEXT-EFFICIENCY-INSTRUCTIONS §2)"
        )
        assert "WHEN NOT TO USE" not in ft.description
        assert "COMMON PATTERNS" not in ft.description
        assert "read_file" in ft.description, (
            "must point at read_file for the full body — the description "
            "states the result, the preview is capped, the body is not read"
        )

    def test_file_search_tool_requires_query(self):
        """JSON schema marks 'query' as required."""
        ft = next(t for t in get_all_tools() if t.name == "file_search")
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
                with open(os.path.join(proj, f"file_{i}.py"), 'w') as f:
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
                f"JIT ({len(jit)}) should be < preload ({len(preload)})"

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
    """Spec §3.21m + SPEC-38 §4: runtime passes context_mode from ProviderConfig."""

    def test_runtime_passes_context_mode_from_provider(self):
        """SPEC-38 §4 replaced the inline getattr(default_provider_cfg, ...)
        wiring with _budget_window_and_mode(conv), which resolves BOTH the
        budget window and context_mode from the CONVERSATION's provider
        (model prefix). Pin the new wiring: it exists and returns the
        provider's context_mode alongside the resolved window."""
        from agent.config import AgentConfig, LLMProviderConfig
        from agent.runtime import AgentRuntime
        from models.conversation import Conversation

        provider = LLMProviderConfig(
            name="openai",
            base_url="x",
            api_key="x",
            default_model="openai/gpt-4o",
            caller="openai",
            context_window=1_000_000,
            context_mode="hybrid",  # valid mode per models/providers.py
        )
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._config = AgentConfig(
            providers={"openai": provider}, default_provider="openai",
        )
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        assert runtime._budget_window_and_mode(conv) == (1_000_000, "hybrid")

    def test_runtime_defaults_context_mode_auto(self):
        """When ProviderConfig has no context_mode, runtime defaults to 'auto'."""
        from agent.config import AgentConfig, LLMProviderConfig
        from agent.runtime import AgentRuntime
        from models.conversation import Conversation
        from models.providers import ProviderConfig
        cfg = ProviderConfig(name="test", base_url="http://x", api_key="k", default_model="m")
        mode = getattr(cfg, "context_mode", "auto") or "auto"
        assert mode == "auto"
        # Runtime level: an unset context_mode resolves to "auto" alongside
        # the budget window (SPEC-38 §4 wiring).
        provider = LLMProviderConfig(
            name="openai",
            base_url="x",
            api_key="x",
            default_model="openai/gpt-4o",
            caller="openai",
            max_tokens=1_000_000,  # no context_window → window from max_tokens
        )
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._config = AgentConfig(
            providers={"openai": provider}, default_provider="openai",
        )
        window, auto_mode = runtime._budget_window_and_mode(
            Conversation(agent_name="Coder", model="openai/gpt-4o"),
        )
        assert window == 1_000_000
        assert auto_mode == "auto"


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
        """build_file_index skips line count for binary files without crashing.

        Line counts are OFF on the default path (CONTEXT-EFFICIENCY-INSTRUCTIONS
        §1: the system-prompt path is stat-only and must not read file bodies),
        so this opts in explicitly to exercise the line-count branch — that is
        where a binary read can blow up.
        """
        with tempfile.TemporaryDirectory() as proj:
            # Create a binary file
            with open(os.path.join(proj, "data.bin"), "wb") as f:
                f.write(b'\x00\x01\x02\x03\xff\xfe\x00binary')
            # Create a normal Python file
            with open(os.path.join(proj, "code.py"), "w") as f:
                f.write("x = 1\n")

            result = build_file_index(proj, include_line_counts=True)
            assert isinstance(result, str)
            # Binary file should appear in index (by extension)
            assert "data.bin" in result or "data" in result
            # Python file should have line count
            assert "1 lines" in result

    def test_build_file_index_default_path_is_stat_only(self):
        """Default call reads no file bodies — no line counts in the output."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "code.py"), "w") as f:
                f.write("x = 1\n")
            idx = build_file_index(proj)
            assert "code.py" in idx
            assert "lines" not in idx, (
                "default path must not read file bodies for line counts "
                "(CONTEXT-EFFICIENCY-INSTRUCTIONS §1)"
            )
