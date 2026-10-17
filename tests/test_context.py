# tests/test_context.py
# Unit tests for agent/context.py
#
# Principles:
#   - Pure functions — no mocks needed for network or GTK
#   - Test real filesystem behavior with tempfile
#   - Test gitignore parsing, truncation, custom prompt loading

import os
import tempfile

import pytest

from agent.context import (
    _load_project_prompts_context,
    build_file_context,
    build_system_prompt,
)


# ═══════════════════════════════════════════════════════════════════
#  build_system_prompt
# ═══════════════════════════════════════════════════════════════════

class TestBuildSystemPrompt:
    def test_contains_agent_name(self):
        with tempfile.TemporaryDirectory() as proj:
            prompt = build_system_prompt("Coder", proj, ["read_file", "write_file"])
            assert "Coder" in prompt

    def test_contains_project_path(self):
        with tempfile.TemporaryDirectory() as proj:
            prompt = build_system_prompt("Coder", proj, [])
            assert proj in prompt

    def test_no_project_shows_placeholder(self):
        prompt = build_system_prompt("Coder", None, [])
        assert "Coder" in prompt or "no project" in prompt.lower()

    def test_tool_list_included(self):
        prompt = build_system_prompt("Coder", "/tmp", ["read_file", "exec_command"])
        assert "read_file" in prompt
        assert "exec_command" in prompt

    def test_review_mode_awareness(self):
        prompt = build_system_prompt("Coder", "/tmp", [], review_mode="review")
        assert "REVIEW MODE ACTIVE" in prompt or "review" in prompt.lower()

    def test_review_mode_off_no_warning(self):
        prompt = build_system_prompt("Coder", "/tmp", [], review_mode="off")
        # Should not contain REVIEW MODE ACTIVE
        assert "REVIEW MODE ACTIVE" not in prompt

    def test_custom_prompt_overrides_template(self):
        """Custom AGENTS.md is no longer supported — template system is used instead."""
        with tempfile.TemporaryDirectory() as proj:
            prompt = build_system_prompt("Coder", proj, ["read_file"])
            # Template system should produce a prompt (not crash)
            assert "Coder" in prompt

    def test_coder_template_has_correct_sections(self):
        prompt = build_system_prompt("Coder", "/tmp", [])
        assert "software engineering" in prompt or "Coder" in prompt

    def test_debugger_template_used_for_debugger(self):
        prompt = build_system_prompt("Debugger", "/tmp", [])
        assert "Debugger" in prompt  # name appears

    def test_supervisor_role_fallback_from_name(self):
        """Supervisor role is derived from the name when no explicit role is given.

        Per SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.5: the fallback role
        derivation must recognize supervisor as defense-in-depth.
        """
        prompt = build_system_prompt("Supervisor", "/tmp", [])
        assert "orchestrator" in prompt.lower()  # supervisor.md content


    def test_collab_prompt_in_all_agents(self):
        """collab.md is composed into all agents regardless of role."""
        p_coder = build_system_prompt("Coder", "/tmp", [])
        p_gateway = build_system_prompt("QTR", "/tmp", [], agent_role="")
        p_debugger = build_system_prompt("Debugger", "/tmp", [], agent_role="debugger")
        for p, label in [(p_coder, "coder"), (p_gateway, "gateway"), (p_debugger, "debugger")]:
            assert "Agent Collaboration" in p, f"collab missing in {label} prompt"

    def test_collab_comes_after_default(self):
        """collab.md loads after default.md in the composition order."""
        prompt = build_system_prompt("Coder", "/tmp", [])
        default_pos = prompt.find("You are")
        collab_pos = prompt.find("Agent Collaboration")
        assert 0 <= default_pos < collab_pos, "collab should come after default"

    def test_collab_in_no_project_prompt(self):
        """collab.md applies even when no project is active."""
        prompt = build_system_prompt("Coder", None, [])
        assert "Agent Collaboration" in prompt, "collab should be in no-project prompt"


# ═══════════════════════════════════════════════════════════════════
#  build_file_context — gitignore
# ═══════════════════════════════════════════════════════════════════

class TestBuildFileContextGitignore:
    def test_gitignore_basic_pattern(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, ".gitignore"), "w") as f:
                f.write("*.pyc\n__pycache__/\n")
            os.makedirs(os.path.join(proj, "__pycache__"))
            with open(os.path.join(proj, "main.pyc"), "w") as f:
                f.write("bytecode")
            with open(os.path.join(proj, "main.py"), "w") as f:
                f.write("source")
            ctx = build_file_context(proj)
            assert "main.py" in ctx
            assert "__pycache__" not in ctx
            assert ".pyc" not in ctx

    def test_gitignore_comment_ignored(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, ".gitignore"), "w") as f:
                f.write("# This is a comment\n*.bak\n")
            with open(os.path.join(proj, "file.bak"), "w") as f:
                f.write("backup")
            with open(os.path.join(proj, "file.py"), "w") as f:
                f.write("source")
            ctx = build_file_context(proj)
            assert "file.py" in ctx
            assert "file.bak" not in ctx

    def test_gitignore_directory_pattern(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, ".gitignore"), "w") as f:
                f.write("node_modules/\n.venv/\n")
            os.makedirs(os.path.join(proj, "node_modules"))
            os.makedirs(os.path.join(proj, "src"))
            with open(os.path.join(proj, "src", "main.py"), "w") as f:
                f.write("source")
            ctx = build_file_context(proj)
            assert "node_modules" not in ctx
            assert "src/" in ctx

    def test_negation_pattern_ignored_due_to_no_support(self):
        """Negation patterns (!) are not supported — ignored files stay ignored."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, ".gitignore"), "w") as f:
                f.write("*.txt\n!important.txt\n")
            with open(os.path.join(proj, "file.py"), "w") as f:
                f.write("python file")
            with open(os.path.join(proj, "important.txt"), "w") as f:
                f.write("important")
            ctx = build_file_context(proj)
            # file.py is shown (not ignored)
            assert "file.py" in ctx
            # important.txt is ignored (negation not supported)
            assert "important.txt" not in ctx

    def test_no_gitignore_all_files_shown(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.txt"), "w") as f:
                f.write("a")
            with open(os.path.join(proj, "b.txt"), "w") as f:
                f.write("b")
            ctx = build_file_context(proj)
            assert "a.txt" in ctx
            assert "b.txt" in ctx

    def test_hidden_files_starting_with_dot_ignored(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, ".env"), "w") as f:
                f.write("SECRET=x")
            with open(os.path.join(proj, "main.py"), "w") as f:
                f.write("source")
            ctx = build_file_context(proj)
            assert ".env" not in ctx
            assert "main.py" in ctx


# ═══════════════════════════════════════════════════════════════════
#  build_file_context — truncation
# ═══════════════════════════════════════════════════════════════════

class TestBuildFileContextTruncation:
    def test_small_context_not_truncated(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "small.txt"), "w") as f:
                f.write("tiny")
            ctx = build_file_context(proj, max_chars=50_000)
            assert "truncated" not in ctx
            assert "small.txt" in ctx

    def test_large_key_files_truncated(self):
        with tempfile.TemporaryDirectory() as proj:
            # README.md (~8KB) + big.txt (~5KB) = ~13KB > 5KB limit
            with open(os.path.join(proj, "README.md"), "w") as f:
                f.write("# Project\n" + "A" * 8000)
            with open(os.path.join(proj, "big.txt"), "w") as f:
                f.write("B" * 5000)
            ctx = build_file_context(proj, max_chars=5000)
            assert "truncated" in ctx
            assert len(ctx) <= 5000 + 100  # within limit + marker

    def test_truncation_marker_at_end(self):
        with tempfile.TemporaryDirectory() as proj:
            # 100 files × ~5 bytes/entry ≈ 547 chars > 500 limit
            for i in range(100):
                with open(os.path.join(proj, f"f{i}.txt"), "w") as f:
                    f.write(str(i))
            ctx = build_file_context(proj, max_chars=500)
            assert "truncated" in ctx

    def test_truncation_marker_counts_inside_the_budget(self):
        """Debugger audit BUG #4: the marker is budget, not an addition to it.

        The tail cut sliced to exactly ``max_chars`` and then appended the
        marker, so the return was ``max_chars + len(marker)`` — 47-49 chars
        over at every budget, because the marker's own length scales with
        the digits of ``max_chars``. Reserve the marker first, then slice to
        whatever budget is left.
        """
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), "w") as f:
                f.write("# Project\n" + "A" * 8000)
            max_chars = 5000
            ctx = build_file_context(proj, max_chars=max_chars)
            assert "truncated" in ctx, "fixture must overflow the budget"
            assert len(ctx) <= max_chars, (
                f"returned {len(ctx)} chars for a {max_chars} budget "
                f"(over by {len(ctx) - max_chars})"
            )

    def test_tight_budget_keeps_tree_and_drops_prompt_body(self):
        """§7: the tail cut keeps the tree. Prompt bodies are dropped first."""
        with tempfile.TemporaryDirectory() as proj:
            prompts = os.path.join(proj, ".crabcakes", "prompts")
            os.makedirs(prompts)
            with open(os.path.join(prompts, "huge.md"), "w") as f:
                f.write("PROMPT-BODY-UNIQUE " + ("P" * 8000))
            with open(os.path.join(proj, "kept_source.py"), "w") as f:
                f.write("x = 1\n")
            ctx = build_file_context(proj, max_chars=2000)
            assert "kept_source.py" in ctx
            assert "PROMPT-BODY-UNIQUE" not in ctx

    def test_docs_beat_prompts_when_budget_cannot_hold_all_three(self):
        """§4.4a + §7: prompts are the sacrificial layer, never the docs.

        Phase 7 audit BUG #2/#3: the prompt budget was derived without the
        docs, so a tight budget pushed the assembly over ``max_chars`` and
        the tail cut deleted the '## Project docs' heading — the one group
        §4.4a says is always included — while the very prompt bodies §7
        says get dropped first survived. With the docs reserved up front
        the overflow is absorbed by prompt bodies instead, so the heading
        and the tree both survive and nothing is tail-cut.
        """
        with tempfile.TemporaryDirectory() as proj:
            crab = os.path.join(proj, ".crabcakes")
            prompts = os.path.join(crab, "prompts")
            os.makedirs(prompts)
            # .crabcakes/context.md is in _read_crabcakes_docs' DOC_NAMES, so
            # this is what puts a '## Project docs' section in the output.
            with open(os.path.join(crab, "context.md"), "w") as f:
                f.write("# Context\n" + "D" * 2_000)
            # Sorted by name; 030_big is the one that cannot fit the budget.
            for name, marker, char, size in (
                ("010_small.md", "PROMPT-010-MARKER", "S", 4_000),
                ("020_mid.md", "PROMPT-020-MARKER", "M", 6_000),
                ("030_big.md", "PROMPT-030-MARKER", "B", 9_000),
            ):
                with open(os.path.join(prompts, name), "w") as f:
                    f.write(marker + "\n" + char * size)
            # A README puts a real '## Key files' section in the tail.
            with open(os.path.join(proj, "README.md"), "w") as f:
                f.write("# Fixture\n" + "R" * 1_500)
            with open(os.path.join(proj, "main.py"), "w") as f:
                f.write("x = 1\n")

            # --- tight budget: docs + tree fit, the prompt set does not ---
            ctx = build_file_context(proj, max_chars=20_000)

            assert "## Project docs" in ctx, "docs dropped under a tight budget"
            assert "## .crabcakes/context.md" in ctx
            assert "## Project tree" in ctx
            assert "main.py" in ctx
            # Sacrificial layer gave way: a prompt body is gone (here the
            # all-or-nothing block would also satisfy "or absent").
            assert "PROMPT-030-MARKER" not in ctx
            # The giveaway that this was a budgeted drop, not a tail cut:
            # a tail cut would have eaten the docs heading and appended the
            # truncation marker instead.
            assert "truncated" not in ctx
            assert ctx.find("## Project docs") < ctx.find("## Project tree")

            # --- comfortable budget: all three groups, docs still first ---
            ctx = build_file_context(proj, max_chars=120_000)

            assert "## Project docs" in ctx
            assert "## Project prompts" in ctx
            assert "## Project tree" in ctx
            for marker in ("PROMPT-010-MARKER", "PROMPT-020-MARKER",
                           "PROMPT-030-MARKER"):
                assert marker in ctx, f"{marker} lost at a comfortable budget"
            docs_pos = ctx.find("## Project docs")
            assert 0 <= docs_pos < ctx.find("## Project tree")
            assert ctx.find("## Project prompts") < ctx.find("## Project tree")


# ═══════════════════════════════════════════════════════════════════
#  build_file_context — key files
# ═══════════════════════════════════════════════════════════════════

class TestBuildFileContextKeyFiles:
    def test_readme_included(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), "w") as f:
                f.write("# My Project\nThis is the readme.")
            ctx = build_file_context(proj)
            assert "README.md" in ctx
            assert "My Project" in ctx

    def test_pyproject_toml_included(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "pyproject.toml"), "w") as f:
                f.write("[project]\nname = \"test\"")
            ctx = build_file_context(proj)
            assert "pyproject.toml" in ctx
            assert "test" in ctx

    def test_file_too_large_shows_placeholder(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), "w") as f:
                f.write("X" * 60_000)  # over 50KB per-file limit
            ctx = build_file_context(proj)
            assert "too large" in ctx
            assert "X" * 60_000 not in ctx  # content not included

    def test_key_files_section_present(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "README.md"), "w") as f:
                f.write("Readme content")
            ctx = build_file_context(proj)
            assert "Key files" in ctx or "README.md" in ctx


# ═══════════════════════════════════════════════════════════════════
#  build_file_context — query mode
# ═══════════════════════════════════════════════════════════════════

class TestBuildFileContextQuery:
    def test_query_matches_file_by_name(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "auth.py"), "w") as f:
                f.write("def login(): pass")
            with open(os.path.join(proj, "main.py"), "w") as f:
                f.write("def main(): pass")
            ctx = build_file_context(proj, query="auth")
            assert "auth.py" in ctx
            assert "login" in ctx
            assert "main.py" not in ctx

    def test_query_no_matches(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), "w") as f:
                f.write("def foo(): pass")
            ctx = build_file_context(proj, query="NOTFOUNDXYZ")
            assert "No files match" in ctx

    def test_query_case_insensitive(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "MyFile.py"), "w") as f:
                f.write("content")
            ctx = build_file_context(proj, query="myfile")
            assert "MyFile.py" in ctx

    def test_query_in_subdirectory(self):
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "src"))
            with open(os.path.join(proj, "src", "utils.py"), "w") as f:
                f.write("def helper(): pass")
            ctx = build_file_context(proj, query="utils")
            assert "utils.py" in ctx


# ═══════════════════════════════════════════════════════════════════
#  build_file_context — directory tree
# ═══════════════════════════════════════════════════════════════════

class TestBuildFileContextTree:
    def test_tree_shows_nested_structure(self):
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "src", "utils"))
            with open(os.path.join(proj, "src", "main.py"), "w") as f:
                f.write("source")
            with open(os.path.join(proj, "src", "utils", "helper.py"), "w") as f:
                f.write("helper source")
            ctx = build_file_context(proj)
            assert "src/" in ctx
            assert "main.py" in ctx
            assert "utils/" in ctx
            assert "helper.py" in ctx

    def test_tree_shows_files_at_root(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "main.py"), "w") as f:
                f.write("source")
            ctx = build_file_context(proj)
            assert "main.py" in ctx


# ═══════════════════════════════════════════════════════════════════
#  Phase CB-2: build_system_prompt model_max_tokens plumbing
# ═══════════════════════════════════════════════════════════════════

from pathlib import Path
from agent.context import build_system_prompt


class TestBuildSystemPromptBudget:
    """Phase CB-2: build_system_prompt passes model_max_tokens through to compose_system_prompt."""

    def test_model_max_is_plumbed_through(self):
        with tempfile.TemporaryDirectory() as proj:
            (Path(proj) / "huge.txt").write_text("x" * 30_000)
            (Path(proj) / "small.txt").write_text("y" * 100)
            prompt_small = build_system_prompt(
                "Coder", proj, [], agent_role="coder", model_max_tokens=500,
            )
            prompt_large = build_system_prompt(
                "Coder", proj, [], agent_role="coder", model_max_tokens=200_000,
            )
            assert len(prompt_small) < len(prompt_large)

    def test_no_model_max_means_no_truncation(self):
        with tempfile.TemporaryDirectory() as proj:
            (Path(proj) / "file.txt").write_text("z" * 5_000)
            prompt = build_system_prompt("Coder", proj, [], agent_role="coder")
            assert "file.txt" in prompt


# ═══════════════════════════════════════════════════════════════════
#  _load_project_prompts_context — budgeted walk
# ═══════════════════════════════════════════════════════════════════

class TestLoadProjectPromptsContextBudget:
    def test_oversized_file_sorting_first_does_not_kill_the_walk(self):
        """A file too big for the remaining budget is SKIPPED, not a stop.

        Phase 7 audit BUG #1: the walk is sorted by filename, not by size,
        so an oversized file sorting first used to break the loop and drop
        every file behind it. All three small files must still load.
        """
        with tempfile.TemporaryDirectory() as proj:
            prompts = os.path.join(proj, ".crabcakes", "prompts")
            os.makedirs(prompts)
            # Sorts first, strictly BIGGER than the 2,000-char budget (equal
            # would still fit and eat the whole budget), yet well under the
            # 20KB per-file cap so it survives the size pre-filter and only
            # the budgeted walk can reject it.
            with open(os.path.join(prompts, "010_big.md"), "w") as f:
                f.write("B" * 3_000)
            for name, body in (("020_small_a.md", "a"), ("030_small_b.md", "b"),
                               ("040_small_c.md", "c")):
                with open(os.path.join(prompts, name), "w") as f:
                    f.write(body * 10)

            out = _load_project_prompts_context(proj, max_chars=2_000)

            for name in ("020_small_a", "030_small_b", "040_small_c"):
                assert f"## .crabcakes/prompts/{name}" in out, f"{name} dropped"
            assert "## .crabcakes/prompts/010_big" not in out


# ═══════════════════════════════════════════════════════════════════
#  build_file_context — preload cache invalidation
# ═══════════════════════════════════════════════════════════════════

class TestBuildFileContextCacheInvalidation:
    def test_subdir_add_invalidates_preload_cache(self):
        """Bugfix C2 (audit BUG #1): a subdirectory add must rebuild.

        The preload cache keyed on ``(project_path, max_chars)`` validated on
        ``_project_root_mtime`` alone — root + 4 fixed files. Adding a file
        inside a directory that already exists leaves that mtime untouched,
        so the second call used to return byte-identical stale text forever.
        The cached tuple now also stores the tree fingerprint, which changes.
        """
        with tempfile.TemporaryDirectory() as proj:
            # pkg/ exists BEFORE the first call: creating it later would
            # itself bump the root mtime and mask the fingerprint guard.
            pkg = os.path.join(proj, "pkg")
            os.makedirs(pkg)
            with open(os.path.join(pkg, "existing.py"), "w") as f:
                f.write("x = 1\n")

            first = build_file_context(proj, max_chars=50_000)
            assert "brand_new_module" not in first, "fixture must start clean"

            with open(os.path.join(pkg, "brand_new_module.py"), "w") as f:
                f.write("y = 2\n")

            second = build_file_context(proj, max_chars=50_000)

            assert "brand_new_module" in second, (
                "subdir add must invalidate the preload cache; "
                "second call served stale text"
            )
            assert second != first, "second output must not be byte-identical"
