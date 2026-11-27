# tests/test_enforcement.py
# Unit tests for SPEC-2: Auto-Test Enforcement Layer
#
# Covers:
#   - TestConfig dataclass + from_dict()
#   - _detect_venv_prefix()
#   - _load_test_config() with TTL cache
#   - _find_related_test() with configurable naming pattern
#   - _check_tests() with TestConfig, venv, custom command, timeout
#   - End-to-end check() with per-project test config
#
# Architecture: enforcement.py is pure Python + subprocess. No GTK, no network.
# Tests use real subprocess calls for integration, temp dirs for isolation.

import json
import os
import shlex
import subprocess
import sys
import time

import pytest

from unittest.mock import patch

from agent.enforcement import (
    TestConfig,
    _APP_ROOT,
    _detect_venv_prefix,
    _find_related_test,
    _is_app_worktree,
    _load_test_config,
    _load_project_enforcement_config,
    _check_tests,
    _resolve_tests_python,
    check,
    EnforcementCheck,
    _TEST_CONFIG_CACHE,
    _ENFORCEMENT_CONFIG_CACHE,
)
from agent.config import EnforcementConfig
from agent.tools import ToolResult

from models.providers import ProviderConfig

import utils.providers_store as ps


def _run_git(cwd, args: list[str]) -> None:
    """Run a git command in *cwd* (SP0 V3 worktree-matrix helper).

    Self-sufficient: for `git init` it initializes FIRST, then sets a
    repo-local identity (the git tests fail with exit 128 — Author identity
    unknown — on hosts without a global git identity).
    """
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "sp0@test.invalid"], cwd=str(cwd),
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "SP0 Test"], cwd=str(cwd),
        check=True, capture_output=True,
    )



# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_config(**overrides) -> EnforcementConfig:
    """Create EnforcementConfig with sensible test defaults."""
    defaults = dict(
        enabled=True,
        syntax_check=True,
        test_run=True,
        lint_check=False,
        test_timeout_seconds=30,
        max_output_chars=2000,
    )
    defaults.update(overrides)
    return EnforcementConfig(**defaults)


def _write_enforcement_json(project_path: str, config: dict) -> str:
    """Write enforcement.json and return the .crabcakes dir path."""
    crab_dir = os.path.join(project_path, ".crabcakes")
    os.makedirs(crab_dir, exist_ok=True)
    cfg_path = os.path.join(crab_dir, "enforcement.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    # Clear caches so the new file is picked up
    _TEST_CONFIG_CACHE.clear()
    _ENFORCEMENT_CONFIG_CACHE.clear()
    return crab_dir


def _create_test_file(project_path: str, test_filename: str, content: str = "def test_ok(): assert True\n") -> str:
    """Create a test file in the project's tests/ directory."""
    tests_dir = os.path.join(project_path, "tests")
    os.makedirs(tests_dir, exist_ok=True)
    path = os.path.join(tests_dir, test_filename)
    with open(path, "w") as f:
        f.write(content)
    return path


def _create_venv(project_path: str, venv_path: str = ".venv") -> str:
    """Create a minimal fake venv with activate script and python binary."""
    venv_bin = os.path.join(project_path, venv_path, "bin")
    os.makedirs(venv_bin, exist_ok=True)
    activate = os.path.join(venv_bin, "activate")
    with open(activate, "w") as f:
        f.write("# fake activate\ntrue\n")
    python = os.path.join(venv_bin, "python")
    with open(python, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(python, 0o755)
    return venv_bin


def _create_real_venv(project_path: str, venv_path: str = ".venv") -> str:
    """Create a venv whose python execs the RUNNING interpreter.

    Unlike _create_venv's exit-0 shim, this python actually executes pytest:
    the shim `exec`s sys.executable directly, so the real interpreter (with
    develcakes' site-packages) runs the tier command for real. A bare
    symlink is NOT enough — getpath finds no pyvenv.cfg beside the symlink
    and falls back to the system prefix, which has no pytest (observed:
    "No module named pytest" via symlink, 2026-09-25).

    Used by tier tests that assert on REAL test outcomes (pass/fail/
    timeout) so a passing tier comes from the tests passing — not from
    the shim. SP6 Phase 2 fix round: with the app-identity gate active,
    foreign (tmp_path) projects no longer fall back to the host
    interpreter, so venv-backed setups are the honest way to exercise
    the running tier (venv-first is the sanctioned contract).
    """
    venv_bin = os.path.join(project_path, venv_path, "bin")
    os.makedirs(venv_bin, exist_ok=True)
    python = os.path.join(venv_bin, "python")
    with open(python, "w") as f:
        f.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    os.chmod(python, 0o755)
    return venv_bin


# ── TestConfig ──────────────────────────────────────────────────────────────


class TestTestConfig:
    """SPEC-2 §6.1 — TestConfig dataclass and from_dict()."""

    def test_from_dict_full(self):
        tc = TestConfig.from_dict({
            "command": "pytest {test_file}",
            "full_suite_command": "pytest tests/",
            "test_dir": "spec",
            "naming_pattern": "{module}_spec.py",
            "venv_path": ".virtualenv",
            "run_full_suite": True,
            "timeout_seconds": 45,
            "extra_args": "-v --tb=long",
        })
        assert tc.command == "pytest {test_file}"
        assert tc.full_suite_command == "pytest tests/"
        assert tc.test_dir == "spec"
        assert tc.naming_pattern == "{module}_spec.py"
        assert tc.venv_path == ".virtualenv"
        assert tc.run_full_suite is True
        assert tc.timeout_seconds == 45
        assert tc.extra_args == "-v --tb=long"

    def test_from_dict_partial(self):
        """Only specified fields override defaults."""
        tc = TestConfig.from_dict({"timeout_seconds": 10, "test_dir": "t"})
        assert tc.command is None
        assert tc.test_dir == "t"
        assert tc.naming_pattern == "test_{module}.py"  # default
        assert tc.timeout_seconds == 10
        assert tc.run_full_suite is False  # default

    def test_from_dict_empty(self):
        tc = TestConfig.from_dict({})
        assert tc.command is None
        assert tc.test_dir == "tests"
        assert tc.timeout_seconds is None

    def test_from_dict_non_dict(self):
        tc = TestConfig.from_dict("not a dict")
        assert tc.command is None
        assert tc.test_dir == "tests"

    def test_from_dict_none(self):
        tc = TestConfig.from_dict(None)
        assert tc.command is None

    def test_defaults(self):
        tc = TestConfig()
        assert tc.command is None
        assert tc.full_suite_command is None
        assert tc.test_dir == "tests"
        assert tc.naming_pattern == "test_{module}.py"
        assert tc.venv_path == ".venv"
        assert tc.run_full_suite is False
        assert tc.timeout_seconds is None
        assert tc.extra_args == "-x -q"

    def test_from_dict_string_false_bool_coercion(self):
        """String 'false' must coerce to False, not be truthy."""
        tc = TestConfig.from_dict({"run_full_suite": "false"})
        assert tc.run_full_suite is False, f'Expected False, got {tc.run_full_suite!r}'

    def test_from_dict_bool_timeout_rejected(self):
        """Boolean values for timeout_seconds must fall back to default (60),
        not pass through as a bool which would crash subprocess.run."""
        tc = TestConfig.from_dict({"timeout_seconds": True})
        assert tc.timeout_seconds == 60, f'Expected 60, got {tc.timeout_seconds!r}'
        assert not isinstance(tc.timeout_seconds, bool)

    def test_from_dict_timeout_zero_preserved(self):
        """timeout_seconds=0 must NOT be swallowed by 'or' fallback."""
        tc = TestConfig.from_dict({"timeout_seconds": 0})
        assert tc.timeout_seconds == 0, f'Expected 0, got {tc.timeout_seconds!r}'

    def test_from_dict_string_thirty_coerced(self):
        """String '30' must coerce to int 30."""
        tc = TestConfig.from_dict({"timeout_seconds": "30"})
        assert tc.timeout_seconds == 30
        assert isinstance(tc.timeout_seconds, int)

    def test_from_dict_no_timeout_key_is_none(self):
        """SPEC-61 A5: absent timeout_seconds → None, so enforcement.py:1215
        falls back to EnforcementConfig.test_timeout_seconds."""
        tc = TestConfig.from_dict({"test_dir": "t"})
        assert tc.timeout_seconds is None

    def test_from_dict_explicit_timeout_preserved(self):
        """SPEC-61 A5: present timeout_seconds is still parsed as an int."""
        tc = TestConfig.from_dict({"timeout_seconds": 5})
        assert tc.timeout_seconds == 5
        assert isinstance(tc.timeout_seconds, int)


class TestSpec61B6ParseCache:
    """SPEC-61 B6 — load_providers() content-keyed _PARSE_CACHE.

    Isolation: conftest's autouse _isolate_config_resolution redirects HOME
    and XDG_CONFIG_HOME into per-test tmp dirs, so the module-level cache
    never crosses tests (its path component differs per test).
    """

    def _make_provider(self, name: str = "test", **overrides) -> ProviderConfig:
        defaults = dict(
            name=name,
            base_url=f"https://api.{name}.example.com/v1",
            api_key=f"sk-{name}-key",
            default_model=f"openai/{name}-model",
        )
        defaults.update(overrides)
        return ProviderConfig(**defaults)

    def test_two_loads_same_file_parse_once(self, tmp_path, monkeypatch):
        ps.save_providers([self._make_provider("alpha")])
        calls = []
        orig_parse = ps._parse

        def spy(text):
            calls.append(1)
            return orig_parse(text)

        monkeypatch.setattr(ps, "_parse", spy)
        first = ps.load_providers()
        second = ps.load_providers()
        assert len(calls) == 1
        assert first[0].name == "alpha"
        assert second[0].name == "alpha"

    def test_save_rewrite_seen_on_next_load(self, tmp_path):
        ps.save_providers([self._make_provider("a")])
        assert ps.load_providers()[0].name == "a"
        ps.save_providers([self._make_provider("b")])
        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].name == "b"

    def test_mutating_returned_card_does_not_affect_next_load(self, tmp_path):
        ps.save_providers([self._make_provider("x")])
        first = ps.load_providers()
        first[0].api_key = "mutated"
        second = ps.load_providers()
        assert second[0].api_key == "sk-x-key"


# ── _detect_venv_prefix ────────────────────────────────────────────────────


class TestVenvDetection:
    """SPEC-2 §6.1 — _detect_venv_prefix(). Phase 0: now returns absolute python path or None."""

    def test_venv_detected(self, tmp_path):
        """Returns absolute python path when venv exists."""
        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("# python placeholder")
        result = _detect_venv_prefix(str(tmp_path), ".venv")
        assert result == str(tmp_path / ".venv" / "bin" / "python")

    def test_no_venv(self, tmp_path):
        """Returns None when no venv exists."""
        result = _detect_venv_prefix(str(tmp_path), ".venv")
        assert result is None

    def test_custom_venv_path(self, tmp_path):
        """Detects venv at custom path."""
        venv = tmp_path / "env" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("# python placeholder")
        result = _detect_venv_prefix(str(tmp_path), "env")
        assert result == str(tmp_path / "env" / "bin" / "python")

    def test_venv_exists_but_no_python(self, tmp_path):
        """Returns None when venv dir exists but no python binary."""
        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True)
        # No python file
        result = _detect_venv_prefix(str(tmp_path), ".venv")
        assert result is None


class TestResolveTestsPython:
    """SP6 Phase 2 cluster A — tests-tier interpreter fallback.

    Bare `python3` on a PEP 668 host has no pytest; when the project-venv
    probe misses, _resolve_tests_python() falls back to the RUNNING
    interpreter — but ONLY when BOTH hold (fix round, Debugger BUG #1):
    the checked project IS the running app (identity gate against env
    bleed into foreign projects), and pytest is importable there
    (fail-closed: otherwise None and the tier behaves as before).
    The production code does `from importlib.util import find_spec` at
    call time, so patching importlib.util.find_spec controls the probe.
    """

    def test_venv_python_wins_unconditionally(self, tmp_path, monkeypatch):
        """The project venv takes precedence — even for a FOREIGN project
        with a pytest-less running interpreter (venv-first is unconditional;
        covers the has-pytest branch too via M6/M7 kill round 2)."""
        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("# python placeholder")
        # Running interpreter: pytest NOT importable (find_spec → None).
        monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
        result = _resolve_tests_python(str(tmp_path), str(venv / "python"))
        assert result == str(venv / "python")

    def test_venv_python_wins_even_with_pytest_available(self, tmp_path):
        """Foreign project WITH venv + running interpreter HAS pytest:
        the venv path still wins (no identity consultation on branch 1).
        M6-kill: mutant consulting identity before the venv branch fails here."""
        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("# python placeholder")
        result = _resolve_tests_python(str(tmp_path), str(venv / "python"))
        assert result == str(venv / "python")

    def test_app_project_falls_back_to_running_interpreter(self, tmp_path, monkeypatch):
        """The running app's own project, no venv, pytest importable in the
        running interpreter → sys.executable (the self-host case)."""
        from agent import enforcement
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(tmp_path))
        result = _resolve_tests_python(str(tmp_path), None)
        assert result == sys.executable

    def test_foreign_project_returns_none(self, tmp_path):
        """Debugger BUG #1 regression — foreign project (not the running
        app's root), no venv, running interpreter HAS pytest → None.
        The tier must SKIP, not substitute develcakes' interpreter (his
        probe: foreign test importing nh3 false-PASSED against the host
        venv's dependency set)."""
        from agent import enforcement
        assert os.path.realpath(str(tmp_path)) != enforcement._APP_ROOT
        result = _resolve_tests_python(str(tmp_path), None)
        assert result is None

    def test_pytestless_interpreter_returns_none(self, tmp_path, monkeypatch):
        """The app's own project, no venv, running interpreter lacks
        pytest → None (argv keeps the historical bare-python3 shape)."""
        from agent import enforcement
        monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(tmp_path))
        result = _resolve_tests_python(str(tmp_path), None)
        assert result is None

    def test_running_exe_missing_on_disk_returns_none(self, tmp_path, monkeypatch):
        """sys.executable path no longer exists (defensive) → None."""
        from agent import enforcement
        monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(tmp_path))
        monkeypatch.setattr(sys, "executable", "/nonexistent/python3")
        result = _resolve_tests_python(str(tmp_path), None)
        assert result is None


# ── _load_test_config ───────────────────────────────────────────────────────


class TestLoadTestConfig:
    """SPEC-2 §6.1 — _load_test_config() with TTL cache."""

    def test_loads_from_enforcement_json(self, tmp_path):
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(json.dumps({
            "test": {"command": "custom-runner {test_file}", "timeout_seconds": 20}
        }))
        _TEST_CONFIG_CACHE.clear()

        tc = _load_test_config(str(tmp_path))
        assert tc is not None
        assert tc.command == "custom-runner {test_file}"
        assert tc.timeout_seconds == 20

    def test_no_crabcakes_dir(self, tmp_path):
        _TEST_CONFIG_CACHE.clear()
        assert _load_test_config(str(tmp_path)) is None

    def test_no_test_section(self, tmp_path):
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(json.dumps({"syntax_check": True}))
        _TEST_CONFIG_CACHE.clear()
        assert _load_test_config(str(tmp_path)) is None

    def test_malformed_json(self, tmp_path):
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text("not valid json")
        _TEST_CONFIG_CACHE.clear()
        assert _load_test_config(str(tmp_path)) is None

    def test_content_change_invalidates_cache(self, tmp_path):
        """CONSOLIDATED Fix 11 (D4): an edited file is picked up immediately.

        This replaces the old test_cache_ttl assertion, which pinned the STALE
        behavior (an edited enforcement.json kept serving the old value for up
        to 30 s). Cache validity is now the file's content hash.
        """
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(json.dumps({
            "test": {"timeout_seconds": 20}
        }))
        _TEST_CONFIG_CACHE.clear()

        tc1 = _load_test_config(str(tmp_path))
        assert tc1.timeout_seconds == 20

        # Update file — no cache clear, no sleep: the NEW value must win.
        (crab_dir / "enforcement.json").write_text(json.dumps({
            "test": {"timeout_seconds": 40}
        }))
        tc2 = _load_test_config(str(tmp_path))
        assert tc2.timeout_seconds == 40

    def test_cache_miss_returns_none(self, tmp_path):
        _TEST_CONFIG_CACHE.clear()
        assert _load_test_config(str(tmp_path)) is None

    def test_from_dict_bool_timeout_via_load(self, tmp_path):
        """Bool True for timeout in enforcement.json → falls back to default 60."""
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(json.dumps({
            "test": {"command": "pytest {test_file}", "timeout_seconds": True}
        }))
        _TEST_CONFIG_CACHE.clear()
        tc = _load_test_config(str(tmp_path))
        assert tc.timeout_seconds == 60


# ── CONSOLIDATED Fix 11 (D4): content-hash cache validity ───────────────────


class TestEnforcementConfigContentHashCache:
    """The loader caches on the file's CONTENT, not on elapsed time.

    An edit to .crabcakes/enforcement.json must be visible on the very next
    call — no cache clear, no sleep — while an unchanged file still comes from
    the cache (the file is re-read; the JSON parse is what is saved).
    """

    def test_test_config_picks_up_content_change_without_clear(self, tmp_path):
        """_load_test_config: new contents win on the next call."""
        _write_enforcement_json(str(tmp_path), {"test": {"timeout_seconds": 20}})
        # _write_enforcement_json clears the caches; from here on, nothing does.
        assert _load_test_config(str(tmp_path)).timeout_seconds == 20

        _write_enforcement_json(str(tmp_path), {"test": {"timeout_seconds": 45}})
        assert _load_test_config(str(tmp_path)).timeout_seconds == 45

    def test_project_config_picks_up_content_change_without_clear(self, tmp_path):
        """_load_project_enforcement_config: new contents win on the next call."""
        _write_enforcement_json(str(tmp_path), {"lint_check": True})
        assert _load_project_enforcement_config(str(tmp_path))["lint_check"] is True

        _write_enforcement_json(str(tmp_path), {"lint_check": False})
        assert _load_project_enforcement_config(str(tmp_path))["lint_check"] is False

    def test_unchanged_file_is_not_reparsed(self, tmp_path):
        """An unchanged file is served from cache — json.loads runs once."""
        _write_enforcement_json(str(tmp_path), {
            "test": {"timeout_seconds": 20}, "test_run": True,
        })

        with patch("agent.enforcement.json.loads", wraps=json.loads) as spy:
            tc1 = _load_test_config(str(tmp_path))
            cfg1 = _load_project_enforcement_config(str(tmp_path))
            assert spy.call_count == 2  # one parse per loader, first time only

            tc2 = _load_test_config(str(tmp_path))
            cfg2 = _load_project_enforcement_config(str(tmp_path))
            assert spy.call_count == 2  # no re-parse: cache hit

        assert tc1 is tc2
        assert cfg1 is cfg2

    def test_missing_file_stays_fail_soft_cached(self, tmp_path):
        """Missing file: both loaders return None and cache the miss."""
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

        assert _load_test_config(str(tmp_path)) is None
        assert _load_project_enforcement_config(str(tmp_path)) is None
        assert _TEST_CONFIG_CACHE[str(tmp_path)] == (None, None)
        assert _ENFORCEMENT_CONFIG_CACHE[str(tmp_path)] == (None, None)

        # Still None, and still cached, on the next call.
        assert _load_test_config(str(tmp_path)) is None
        assert _load_project_enforcement_config(str(tmp_path)) is None


# ── _find_related_test ──────────────────────────────────────────────────────


class TestFindRelatedTestConfigurable:
    """SPEC-2 §6.1 — _find_related_test() with configurable naming."""

    def test_default_pattern(self, tmp_path):
        """Default pattern finds tests/test_{module}.py."""
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_watcher.py").write_text("# test")

        result = _find_related_test("watcher.py", str(tmp_path))
        assert result is not None
        assert result == "tests/test_watcher.py"

    def test_custom_naming_pattern(self, tmp_path):
        """Finds test file using custom naming pattern."""
        test_dir = tmp_path / "spec"
        test_dir.mkdir()
        (test_dir / "watcher_spec.py").write_text("# test")

        result = _find_related_test(
            "src/watcher.py", str(tmp_path),
            test_dir="spec", naming_pattern="{module}_spec.py",
        )
        assert result is not None
        assert "watcher_spec.py" in result

    def test_custom_test_dir(self, tmp_path):
        """Finds test file in custom test directory."""
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "test_watcher.py").write_text("# test")

        result = _find_related_test(
            "watcher.py", str(tmp_path),
            test_dir="test",
        )
        assert result is not None
        assert "test_watcher.py" in result

    def test_no_matching_test(self, tmp_path):
        """Returns None when no test file found."""
        result = _find_related_test("watcher.py", str(tmp_path))
        assert result is None

    def test_same_directory_test(self, tmp_path):
        """Finds test in same directory as source file."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "test_utils.py").write_text("# test")

        result = _find_related_test("src/utils.py", str(tmp_path))
        assert result is not None
        assert "test_utils.py" in result

    def test_contest_file_not_skipped(self, tmp_path):
        """Files with 'test_' in the name but NOT starting with 'test_' are NOT skipped.

        Regression test: 'contest_results.py', 'protest_handler.py', 'latest_update.py'
        must NOT be skipped by the test-file guard.
        """
        for filename in ("contest_results.py", "protest_handler.py", "latest_update.py"):
            result = _find_related_test(filename, str(tmp_path))
            assert result is None, f"{filename} unexpectedly found a test — shouldn't exist"
        # These files must NOT match the skip guard in _check_tests.
        # Verify the skip guard uses startswith, not contains.
        from agent.enforcement import _check_tests, _TEST_CONFIG_CACHE, _ENFORCEMENT_CONFIG_CACHE
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()
        config = _make_config()
        for filename in ("contest_results.py", "protest_handler.py", "latest_update.py"):
            result = _check_tests(filename, str(tmp_path), config, syntax_passed=True)
            # Returns None because no test framework exists, NOT because it was skipped
            assert result is None, f"{filename} was incorrectly skipped: {result}"


# ── _check_tests ────────────────────────────────────────────────────────────


class TestCheckTests:
    """SPEC-2 — _check_tests() with TestConfig, venv, custom command."""

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def test_no_framework_no_config_skips(self, tmp_path):
        """No test framework detected, no custom command → skip."""
        config = _make_config()
        result = _check_tests("watcher.py", str(tmp_path), config, syntax_passed=True)
        assert result is None

    def test_syntax_failed_skips(self, tmp_path):
        """Syntax failure gates test tier."""
        config = _make_config()
        result = _check_tests("watcher.py", str(tmp_path), config, syntax_passed=False)
        assert result is None

    def test_test_file_itself_skipped(self, tmp_path):
        """Test files are not tested against other tests."""
        config = _make_config()
        result = _check_tests("test_watcher.py", str(tmp_path), config, syntax_passed=True)
        assert result is None

    def test_skipped_pattern_skips(self, tmp_path):
        """Files matching skip patterns are skipped."""
        config = _make_config()
        result = _check_tests("README.md", str(tmp_path), config, syntax_passed=True)
        assert result is None

    def test_custom_command_passing(self, tmp_path):
        """Custom command from enforcement.json runs and passes."""
        _create_real_venv(str(tmp_path))
        _write_enforcement_json(str(tmp_path), {
            "test": {
                "command": "python3 -m pytest {test_file} -v --tb=short",
                "venv_path": ".venv",
            }
        })
        _create_test_file(str(tmp_path), "test_demo.py", "def test_ok(): assert True\n")
        # Write the source file
        with open(os.path.join(str(tmp_path), "demo.py"), "w") as f:
            f.write("x = 1\n")

        config = _make_config()
        result = _check_tests("demo.py", str(tmp_path), config, syntax_passed=True)
        assert result is not None
        assert result.passed is True
        assert "test_demo.py" in result.detail

    def test_custom_command_failing(self, tmp_path):
        """Custom command from enforcement.json runs and detects failure."""
        _create_real_venv(str(tmp_path))
        _write_enforcement_json(str(tmp_path), {
            "test": {
                "command": "python3 -m pytest {test_file} -v --tb=short",
                "venv_path": ".venv",
            }
        })
        _create_test_file(str(tmp_path), "test_broken.py", "def test_fail(): assert False\n")
        with open(os.path.join(str(tmp_path), "broken.py"), "w") as f:
            f.write("x = 1\n")

        config = _make_config()
        result = _check_tests("broken.py", str(tmp_path), config, syntax_passed=True)
        assert result is not None
        assert result.passed is False
        assert "FAILED" in result.detail

    def test_foreign_project_env_bleed_regression(self, tmp_path):
        """Debugger BUG #1, tier-level — foreign project + host-only dep.

        A project that is NOT the running app, with no venv, whose test
        imports a dependency that exists only in the HOST venv (nh3): the
        tier must NOT substitute sys.executable. It runs bare `python3 -m
        pytest` (identity gate → None), which on this PEP 668 host has no
        pytest → tier FAILED — never a false PASS from develcakes'
        dependency set. Pre-fix, this setup false-PASSED via the venv
        fallback (the mutant Debugger killed by probe).
        """
        _create_test_file(
            str(tmp_path), "test_demo.py",
            "import nh3\n\ndef test_uses_host_dep():\n    assert nh3 is not None\n",
        )
        with open(os.path.join(str(tmp_path), "demo.py"), "w") as f:
            f.write("x = 1\n")
        # pytest.ini routes _detect_test_framework to the auto-detect path
        # (no enforcement.json command), the exact shape of Debugger's probe.
        with open(os.path.join(str(tmp_path), "pytest.ini"), "w") as f:
            f.write("[pytest]\n")

        config = _make_config()
        result = _check_tests("demo.py", str(tmp_path), config, syntax_passed=True)
        assert result is not None
        assert result.passed is False, (
            f"foreign project ran with host deps — env bleed: {result.detail}"
        )
        assert "FAILED" in result.detail

    def test_no_related_test_skips(self, tmp_path):
        """No related test found and run_full_suite=false → skip."""
        _write_enforcement_json(str(tmp_path), {
            "test": {"command": "python3 -m pytest {test_file} -v --tb=short"}
        })
        config = _make_config()
        result = _check_tests("orphan.py", str(tmp_path), config, syntax_passed=True)
        assert result is None

    def test_venv_prefix_prepended(self, tmp_path):
        """When venv exists, activation prefix is prepended to test command."""
        _create_venv(str(tmp_path))
        _write_enforcement_json(str(tmp_path), {
            "test": {
                "command": "python3 -m pytest {test_file} -v --tb=short",
                "venv_path": ".venv",
            }
        })
        _create_test_file(str(tmp_path), "test_vdemo.py", "def test_ok(): assert True\n")
        with open(os.path.join(str(tmp_path), "vdemo.py"), "w") as f:
            f.write("x = 1\n")

        config = _make_config()
        result = _check_tests("vdemo.py", str(tmp_path), config, syntax_passed=True)
        assert result is not None
        assert result.passed is True

    def test_configurable_timeout(self, tmp_path):
        """Per-project timeout override is used."""
        _create_real_venv(str(tmp_path))
        _write_enforcement_json(str(tmp_path), {
            "test": {
                "command": "python3 -m pytest {test_file} -v",
                "venv_path": ".venv",
                "timeout_seconds": 1,
            }
        })
        _create_test_file(str(tmp_path), "test_slow.py",
                          "import time\ndef test_slow(): time.sleep(5)\n")
        with open(os.path.join(str(tmp_path), "slow.py"), "w") as f:
            f.write("x = 1\n")

        config = _make_config()
        result = _check_tests("slow.py", str(tmp_path), config, syntax_passed=True)
        assert result is not None
        assert result.passed is False
        assert "timed out" in result.detail


# ── End-to-end: check() ────────────────────────────────────────────────────


class TestCheckEndToEnd:
    """SPEC-2 — Full check() pipeline with per-project test config."""

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def test_check_with_custom_test_command(self, tmp_path):
        """check() loads per-project test config and runs tests."""
        _create_real_venv(str(tmp_path))
        _write_enforcement_json(str(tmp_path), {
            "syntax_check": True,
            "test_run": True,
            "lint_check": False,
            "test": {
                "command": "python3 -m pytest {test_file} -v --tb=short",
                "venv_path": ".venv",
                "test_dir": "tests",
                "timeout_seconds": 10,
            }
        })
        _create_test_file(str(tmp_path), "test_myapp.py", "def test_ok(): assert True\n")
        with open(os.path.join(str(tmp_path), "myapp.py"), "w") as f:
            f.write("x = 1\n")

        config = _make_config()
        result = check(
            "write_file",
            {"path": "myapp.py"},
            ToolResult(success=True, output="written", error="", duration_ms=10,
                       stdout="", stderr="", exit_code=0),
            str(tmp_path),
            config,
        )
        assert len(result.checks) == 2  # syntax + tests
        assert all(c.passed for c in result.checks)

    def test_no_double_venv_activation(self, tmp_path):
        """Template commands must NOT contain hardcoded activation.

        Both template and crabwatch enforcement.json use plain pytest commands.
        venv activation is handled exclusively by _detect_venv_prefix() → venv_prefix.
        Combining both would produce '. .venv/bin/activate && . .venv/bin/activate && ...'.
        """
        # Verify crabwatch enforcement.json has no activate in command.
        # crabwatch is a separate repo used as a real-world fixture here, so
        # the test skips when it isn't present rather than failing on a
        # hardcoded path. Override with $CRABWATCH_ENFORCEMENT_JSON.
        import json
        crabwatch_cfg = os.environ.get(
            "CRABWATCH_ENFORCEMENT_JSON",
            os.path.join(
                os.path.expanduser("~"), "projects", "crabwatch",
                ".crabcakes", "enforcement.json",
            ),
        )
        if not os.path.isfile(crabwatch_cfg):
            pytest.skip(f"crabwatch enforcement.json not found at {crabwatch_cfg}")
        with open(crabwatch_cfg) as f:
            cfg = json.load(f)
        cmd = cfg["test"]["command"]
        assert "activate" not in cmd, f"crabwatch command contains 'activate': {cmd}"
        assert cmd == "python3 -m pytest {test_file} -v --tb=short"

        # Verify template also has no activate
        # (os is imported at module level; no local import here, or it would
        # shadow the module-level name and break earlier uses in this function)
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docs", "templates", "enforcement-template.json"
        )
        with open(template_path) as f:
            tmpl = json.load(f)
        tmpl_cmd = tmpl["test"]["command"]
        assert "activate" not in tmpl_cmd, f"template command contains 'activate': {tmpl_cmd}"
        assert tmpl_cmd == "python3 -m pytest {test_file} -v --tb=short"

    def test_b5_tests_and_lint_run_concurrently(self, tmp_path):
        """SPEC-61 B5: tests and lint run concurrently once syntax passes."""
        config = _make_config(syntax_check=False, test_run=True, lint_check=True)

        def slow_tests(*_a, **_k):
            time.sleep(0.3)
            return EnforcementCheck(
                tier="tests", tool="write_file", file="myapp.py",
                passed=True, detail="ok", output="", duration_ms=300,
            )

        def slow_lint(*_a, **_k):
            time.sleep(0.3)
            return EnforcementCheck(
                tier="lint", tool="write_file", file="myapp.py",
                passed=True, detail="ok", output="", duration_ms=300,
            )

        with patch("agent.enforcement._check_tests", side_effect=slow_tests), \
             patch("agent.enforcement._check_lint", side_effect=slow_lint):
            start = time.monotonic()
            result = check(
                "write_file",
                {"path": "myapp.py"},
                ToolResult(success=True, output="written", error="", duration_ms=10,
                           stdout="", stderr="", exit_code=0),
                str(tmp_path),
                config,
            )
            elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"tests+lint should overlap: {elapsed:.3f}s"
        assert [c.tier for c in result.checks] == ["tests", "lint"]

    def test_check_non_write_tool_skips(self, tmp_path):
        """check() returns empty result for non-write tools."""
        config = _make_config()
        result = check(
            "read_file",
            {"path": "myapp.py"},
            ToolResult(success=True, output="contents", error="", duration_ms=10,
                       stdout="", stderr="", exit_code=0),
            str(tmp_path),
            config,
        )
        assert len(result.checks) == 0

    def test_check_failed_write_skips(self, tmp_path):
        """check() returns empty result when tool itself failed."""
        config = _make_config()
        result = check(
            "write_file",
            {"path": "myapp.py"},
            ToolResult(success=False, output="", error="disk full", duration_ms=10,
                       stdout="", stderr="", exit_code=1),
            str(tmp_path),
            config,
        )
        assert len(result.checks) == 0

    # ── verbose mode: SKIPPED entries ────────────────────────────────────

    def test_verbose_false_skipped_files_absent(self, tmp_path):
        """Default behavior (verbose=False): skipped files do NOT appear in output.

        A .md file matches the default skip patterns, so all three tiers
        return None. result.checks must be empty and appended_message empty.
        This guarantees backward compatibility for existing callers.
        """
        config = _make_config()
        # Create a markdown file that matches EnforcementConfig.skip_patterns
        # (agent/config.py) — the default skip-pattern list.
        md_path = tmp_path / "README.md"
        md_path.write_text("# Title\n")

        result = check(
            "write_file",
            {"path": "README.md"},
            ToolResult(success=True, output="written", error="", duration_ms=10,
                       stdout="", stderr="", exit_code=0),
            str(tmp_path),
            config,
            verbose=False,
        )
        assert result.checks == []
        assert result.appended_message == ""

    def test_verbose_true_skipped_files_appear(self, tmp_path):
        """verbose=True: skipped files appear with a SKIPPED: prefix.

        A .md file matches the default skip patterns, so the tests tier
        (which reaches the skip check) produces a SKIPPED placeholder
        check with the SKIPPED: prefix in its detail. Other tiers
        (syntax, lint) may also skip — but for unrelated reasons
        (unknown extension, no linter configured for .md) and may
        return None before even consulting the skip patterns. The
        contract the user asked for is: skipped files DO appear in the
        output with a SKIPPED prefix, vs. NOT appearing at all in
        verbose=False mode.
        """
        config = _make_config()
        md_path = tmp_path / "README.md"
        md_path.write_text("# Title\n")

        result = check(
            "write_file",
            {"path": "README.md"},
            ToolResult(success=True, output="written", error="", duration_ms=10,
                       stdout="", stderr="", exit_code=0),
            str(tmp_path),
            config,
            verbose=True,
        )
        # The tests tier must produce a SKIPPED placeholder
        skipped = [c for c in result.checks if c.detail.startswith("SKIPPED:")]
        assert len(skipped) >= 1, f"Expected at least one SKIPPED check, got {result.checks}"
        # Every SKIPPED check must reference the file and the SKIPPED prefix
        for c in skipped:
            assert "README.md" in c.detail
            assert c.passed is True  # SKIPPED is not a failure
        # appended_message includes the file path and the SKIPPED marker
        assert "SKIPPED" in result.appended_message
        assert "README.md" in result.appended_message

    def test_verbose_default_is_false(self, tmp_path):
        """Calling check() without an explicit verbose arg preserves old behavior.

        Regression guard: if someone changes the default to True, this test
        will fail. The contract is 'no behavior change for existing callers'.
        """
        config = _make_config()
        (tmp_path / "CHANGELOG.md").write_text("# changes\n")

        # No verbose kwarg — relies on the default
        result = check(
            "write_file",
            {"path": "CHANGELOG.md"},
            ToolResult(success=True, output="written", error="", duration_ms=10,
                       stdout="", stderr="", exit_code=0),
            str(tmp_path),
            config,
        )
        assert result.checks == []
        assert result.appended_message == ""

    def test_verbose_true_does_not_affect_non_skipped_files(self, tmp_path):
        """verbose=True must not change behavior for files that DO get checked.

        A .py file with a passing syntax check: with verbose=True we still
        get a real syntax check (not a SKIPPED placeholder). The tier
        function should not turn a real check into a placeholder just
        because verbose is on.
        """
        config = _make_config()
        py_path = tmp_path / "module.py"
        py_path.write_text("x = 1\n")

        result = check(
            "write_file",
            {"path": "module.py"},
            ToolResult(success=True, output="written", error="", duration_ms=10,
                       stdout="", stderr="", exit_code=0),
            str(tmp_path),
            config,
            verbose=True,
        )
        # At least one real syntax check (not SKIPPED) must be present
        syntax_checks = [c for c in result.checks if c.tier == "syntax"]
        assert len(syntax_checks) == 1
        assert not syntax_checks[0].detail.startswith("SKIPPED:")
        # The check actually ran and is a real pass
        assert syntax_checks[0].passed is True

    def test_verbose_true_test_file_appears_as_skipped(self, tmp_path):
        """A file that IS a test file is skipped at the tests tier with SKIPPED: prefix
        in verbose mode."""
        config = _make_config()
        (tmp_path / "test_helper.py").write_text("def test_x(): assert True\n")

        result = check(
            "write_file",
            {"path": "test_helper.py"},
            ToolResult(success=True, output="written", error="", duration_ms=10,
                       stdout="", stderr="", exit_code=0),
            str(tmp_path),
            config,
            verbose=True,
        )
        # The tests tier must produce a SKIPPED placeholder for the test file
        test_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(test_checks) == 1
        assert test_checks[0].detail.startswith("SKIPPED:")
        assert "test_helper.py" in test_checks[0].detail


# ── SPEC-09 SP0: enforcement hardening (V1/V2/V3) ──────────────────────────
# Verified-live vectors (probes 2026-10-02, pre-fix evidence in the SP0
# report): V1 PATH-bleed (user-PATH shim executed with the scrubbed env,
# marker touched, tier false-PASSED); V2 fake-venv (whole-.venv-dir symlink
# → foreign project ran `import nh3` against develcakes' dependency set,
# false-PASSED). Each SP0 test reproduces its failure mode through the REAL
# check() path where practical — not just the helper.

class TestIsAppWorktree:
    """SP0 V3 — _is_app_worktree 4-case matrix (pre-flight ruling D2).

    SP2 wires the runtime to exec inside <repo>/.worktrees/<agent_id>; this
    gate is the contract SP2 builds on. Case (b) uses a REAL git worktree.
    """

    def _patch_app_root(self, monkeypatch, root):
        from agent import enforcement
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(root))

    def test_a_outside_app_is_false(self, tmp_path):
        """(a) tmp dir NOT under the app → False."""
        assert _is_app_worktree(str(tmp_path)) is False

    def test_b_real_git_worktree_is_true(self, tmp_path, monkeypatch):
        """(b) a REAL git worktree at <app>/.worktrees/<agent> → True.

        Builds a tiny repo, `git worktree add .worktrees/test-agent`,
        patches _APP_ROOT to the repo, asserts the gate passes.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, ["init", "-q"])
        _run_git(repo, ["commit", "-q", "--allow-empty", "-m", "init"])
        _run_git(repo, ["worktree", "add", ".worktrees/test-agent", "-b", "wt"])
        worktree = repo / ".worktrees" / "test-agent"
        assert (worktree / ".git").is_file()  # real worktree marker
        self._patch_app_root(monkeypatch, repo)
        assert _is_app_worktree(str(worktree)) is True

    def test_c_sibling_named_like_worktree_is_false(self, tmp_path, monkeypatch):
        """(c) sibling dir named like a worktree but OUTSIDE .worktrees/ → False."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".worktrees").mkdir()
        fake = repo / "test-agent"          # sibling of .worktrees, same name
        fake.mkdir()
        self._patch_app_root(monkeypatch, repo)
        assert _is_app_worktree(str(fake)) is False

    def test_c2_deep_path_is_false(self, tmp_path, monkeypatch):
        """(c2) a dir INSIDE .worktrees but deeper than one level → False."""
        repo = tmp_path / "repo"
        repo.mkdir()
        deep = repo / ".worktrees" / "agent" / "sub"
        deep.mkdir(parents=True)
        self._patch_app_root(monkeypatch, repo)
        assert _is_app_worktree(str(deep)) is False

    def test_d_symlink_into_worktrees_dir_is_false(self, tmp_path, monkeypatch):
        """(d) symlink from elsewhere INTO the app's .worktrees → False.

        The gate realpaths the CHILD; a link whose TARGET is a real
        worktree resolves to the worktree itself... which IS a member —
        so the docstring contract pins the rule that matters here: the
        parent must be realpath(<app>/.worktrees). A symlinked CHILD that
        resolves to a real worktree passes (it IS that worktree); a symlink
        whose parent chain escapes .worktrees must not. This test plants a
        symlink DIR whose realpath is outside .worktrees, placed so the
        naive (non-realpath) parent compare would pass.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_parent = repo / ".worktrees"
        wt_parent.mkdir()
        # A dir outside .worktrees whose realpath is itself (no symlink):
        # placed at <repo>/decoy. The naive containment
        # `path.startswith(<app>/.worktrees)` would be satisfied by a
        # similarly-named sibling; commonpath on realpaths is not fooled.
        decoy = repo / "decoy"
        decoy.mkdir()
        self._patch_app_root(monkeypatch, repo)
        assert _is_app_worktree(str(decoy)) is False

    def test_d2_symlink_escape_via_link_inside_app(self, tmp_path, monkeypatch):
        """(d2) symlink planted INSIDE the app pointing elsewhere: the child
        realpaths OUT of .worktrees → False (no symlink escape)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_parent = repo / ".worktrees"
        wt_parent.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = wt_parent / "agent-link"
        link.symlink_to(elsewhere, target_is_directory=True)
        self._patch_app_root(monkeypatch, repo)
        assert _is_app_worktree(str(link)) is False

    def test_e_worktrees_parent_symlinked_outside_app_denies_all(self, tmp_path, monkeypatch):
        """(e) THE parent-escape hole (probe-verified OPEN pre-fix): if
        <app>/.worktrees ITSELF is a symlink to a dir outside the app,
        its children must NOT become members. The parent must resolve
        under _APP_ROOT."""
        repo = tmp_path / "repo"
        repo.mkdir()
        evil = tmp_path / "evil"
        agent_dir = evil / "agent"
        agent_dir.mkdir(parents=True)
        (repo / ".worktrees").symlink_to(evil, target_is_directory=True)
        self._patch_app_root(monkeypatch, repo)
        assert _is_app_worktree(str(agent_dir)) is False

    def test_real_worktree_without_git_still_passes_gate(self, tmp_path, monkeypatch):
        """The gate is LAYOUT-based, not git-based: any direct child dir of
        realpath(<app>/.worktrees) passes. Git membership is SP2's job
        (worktree_manager); this gate only bounds WHERE exec may happen.
        Rationale: the identity question for V2/V3 is path topology, and
        requiring `git worktree list` parse here would make the gate
        subprocess-dependent for every enforcement check."""
        repo = tmp_path / "repo"
        repo.mkdir()
        member = repo / ".worktrees" / "agent"
        member.mkdir(parents=True)
        self._patch_app_root(monkeypatch, repo)
        assert _is_app_worktree(str(member)) is True

    def test_worktree_gate_composes_with_identity_gate(self, tmp_path, monkeypatch):
        """End-to-end composition: a real git worktree of a project whose
        _APP_ROOT is the repo resolves tests python via the running
        interpreter (no venv), exactly as the app root itself would."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, ["init", "-q"])
        _run_git(repo, ["commit", "-q", "--allow-empty", "-m", "init"])
        _run_git(repo, ["worktree", "add", ".worktrees/test-agent", "-b", "wt"])
        self._patch_app_root(monkeypatch, repo)
        worktree = repo / ".worktrees" / "test-agent"
        monkeypatch.setattr(
            "importlib.util.find_spec", lambda name: object() if name == "pytest" else None
        )
        result = _resolve_tests_python(str(worktree), None)
        assert result == sys.executable


class TestV2VenvRealpathValidation:
    """SP0 V2 — fake-venv via symlink, refused fail-closed."""

    def _patch_app_root(self, monkeypatch, root):
        from agent import enforcement
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(root))

    def _make_foreign_symlink_project(self, tmp_path, app_root):
        proj = tmp_path / "foreign_link"
        proj.mkdir()
        (proj / "tests").mkdir()
        os.symlink(app_root / ".venv", proj / ".venv")
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "tests" / "test_x.py").write_text(
            "import nh3\n\ndef test_host_dep():\n    assert nh3 is not None\n"
        )
        return proj

    def test_symlinked_app_venv_returns_none(self, tmp_path, monkeypatch):
        """THE vector: foreign project + .venv symlinked to the app venv →
        _resolve_tests_python returns None (not sys.executable, not the
        foreign path). Pre-fix probe: nh3 false-PASSED."""
        app = tmp_path / "app"
        app.mkdir()
        (app / ".venv" / "bin").mkdir(parents=True)
        (app / ".venv" / "bin" / "python").write_text("# interpreter\n")
        self._patch_app_root(monkeypatch, app)
        proj = self._make_foreign_symlink_project(tmp_path, app)
        assert os.path.islink(proj / ".venv")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None, (
            f"symlinked app venv accepted — env bleed: {result}"
        )
        assert _resolve_tests_python(str(proj), result) is None

    def test_own_venv_control_still_used(self, tmp_path, monkeypatch):
        """Control: foreign project with its OWN venv (real dir, copied not
        linked) → its python is used (venv-first is the sanctioned contract)."""
        app = tmp_path / "app"
        app.mkdir()
        (app / ".venv" / "bin").mkdir(parents=True)
        self._patch_app_root(monkeypatch, app)
        proj = tmp_path / "own_venv"
        proj.mkdir()
        own_bin = proj / ".venv" / "bin"
        own_bin.mkdir(parents=True)
        (own_bin / "python").write_text("# interpreter\n")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result == str(own_bin / "python")

    def test_app_project_own_venv_unchanged(self, tmp_path, monkeypatch):
        """The app itself (realpath == _APP_ROOT) keeps its venv."""
        app = tmp_path / "app"
        app.mkdir()
        (app / ".venv" / "bin").mkdir(parents=True)
        (app / ".venv" / "bin" / "python").write_text("# interpreter\n")
        self._patch_app_root(monkeypatch, app)
        result = _detect_venv_prefix(str(app), ".venv")
        assert result == str(app / ".venv" / "bin" / "python")

    def test_app_worktree_with_symlinked_venv_allowed(self, tmp_path, monkeypatch):
        """Composition: an APP WORKTREE with .venv symlinked to the app venv
        is the LEGITIMATE shape (SP2 worktrees share the app's venv — the
        identity gate admits the worktree, so the venv symlink is fine)."""
        app = tmp_path / "app"
        app.mkdir()
        (app / ".venv" / "bin").mkdir(parents=True)
        (app / ".venv" / "bin" / "python").write_text("# interpreter\n")
        self._patch_app_root(monkeypatch, app)
        (app / ".worktrees").mkdir()
        wt = app / ".worktrees" / "agent"
        wt.mkdir()
        os.symlink(app / ".venv", wt / ".venv")
        result = _detect_venv_prefix(str(wt), ".venv")
        assert result == str(wt / ".venv" / "bin" / "python")

    def test_foreign_venv_python_symlink_into_app_bin_refused(self, tmp_path, monkeypatch):
        """The python-symlink shape (probe V2 first draft): foreign project's
        own .venv dir with bin/python → app venv python is ALSO refused
        (both shapes covered: dir symlink AND python symlink).

        SP0 fix round re-fixture: the app venv's bin/python is itself a
        SYMLINK to /usr/bin/python3 (the real-venv shape — the previous
        write_text regular file fabricated an impossible venv and is what
        let the dead literal clause-2 look alive). Under the claim scan the
        refusal comes from the single-hop chain walk: hop 1 =
        <app>/.venv/bin/python (inside the app venv) regardless of what the
        chain ultimately resolves to."""
        app = tmp_path / "app"
        app.mkdir()
        bin_dir = app / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        os.symlink("/usr/bin/python3", bin_dir / "python")
        self._patch_app_root(monkeypatch, app)
        proj = tmp_path / "foreign_pylink"
        proj.mkdir()
        fbin = proj / ".venv" / "bin"
        fbin.mkdir(parents=True)
        os.symlink(bin_dir / "python", fbin / "python")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None


class TestVenvAppClaimScan:
    """SP0 fix round (Debugger BUG#1/#2) — the venv claim matrix.

    A foreign project's venv is refused when ANY surface resolves into the
    app's own venv: dir containment, interpreter symlink HOPS (single-hop
    chain walk, not realpath — realpath collapses a multi-hop chain past
    the intermediate claim), pyvenv.cfg home/executable (literal form,
    resolved form, and hop chain), and site-packages (BUG#1's false-PASS
    surface). Controls pin clean venvs and the app-worktree exemption.
    """

    def _setup_app(self, tmp_path, monkeypatch):
        from agent import enforcement
        app = tmp_path / "app"
        avb = app / ".venv" / "bin"
        avb.mkdir(parents=True)
        # Realistic app interpreter: bin/python -> /usr/bin/python3.
        os.symlink("/usr/bin/python3", avb / "python")
        app_sp = app / ".venv" / "lib" / "python3.12" / "site-packages"
        app_sp.mkdir(parents=True)
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(app))
        return app, avb, app_sp

    def _foreign(self, tmp_path, name):
        proj = tmp_path / name
        bin_dir = proj / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        os.symlink("/usr/bin/python3", bin_dir / "python")
        with open(proj / ".venv" / "pyvenv.cfg", "w") as f:
            f.write("home = /usr/bin\nversion = 3.12.3\n")
        return proj

    def test_b1_site_packages_symlink_refused(self, tmp_path, monkeypatch):
        """BUG#1 probe: foreign venv, OWN pyvenv.cfg, site-packages symlinked
        into the app venv → refused (site-packages named as the claim)."""
        _, _, app_sp = self._setup_app(tmp_path, monkeypatch)
        proj = self._foreign(tmp_path, "f1")
        os.makedirs(proj / ".venv" / "lib" / "python3.12")
        os.symlink(app_sp, proj / ".venv" / "lib" / "python3.12" / "site-packages")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None, (
            "site-packages symlink into app venv accepted — dependency-set "
            f"bleed: {result}"
        )
        from agent.enforcement import _venv_app_claims
        claims = _venv_app_claims(str(proj), ".venv")
        assert any(surface == "site-packages" for surface, _ in claims)

    def test_b1_site_packages_glob_fallback_refused(self, tmp_path, monkeypatch):
        """BUG#1 variant: cfg ``version`` absent/lying → the
        lib/python*/site-packages glob still finds the symlinked dir."""
        _, _, app_sp = self._setup_app(tmp_path, monkeypatch)
        proj = self._foreign(tmp_path, "f1g")
        with open(proj / ".venv" / "pyvenv.cfg", "w") as f:
            f.write("home = /usr/bin\n")  # NO version key
        os.makedirs(proj / ".venv" / "lib" / "python3.99")
        os.symlink(
            app_sp, proj / ".venv" / "lib" / "python3.99" / "site-packages"
        )
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None, (
            "site-packages glob fallback missed an app-venv claim"
        )

    def test_b1_pyvenv_cfg_executable_literal_into_app_venv_refused(self, tmp_path, monkeypatch):
        """BUG#1 companion: pyvenv.cfg ``executable`` = literal path INSIDE
        the app venv (written form claims the app's files even when the
        file is itself a symlink resolving outside)."""
        _, avb, _ = self._setup_app(tmp_path, monkeypatch)
        proj = self._foreign(tmp_path, "f3")
        with open(proj / ".venv" / "pyvenv.cfg", "a") as f:
            f.write(f"executable = {os.path.join(avb, 'python')}\n")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None, "pyvenv.cfg executable literal into app venv accepted"

    def test_b1_pyvenv_cfg_home_resolved_into_app_venv_refused(self, tmp_path, monkeypatch):
        """pyvenv.cfg ``home`` = a symlink INSIDE the foreign venv that
        RESOLVES into the app venv bin — resolved-form + hop coverage."""
        _, avb, _ = self._setup_app(tmp_path, monkeypatch)
        proj = self._foreign(tmp_path, "f3b")
        link_dir = proj / ".venv" / "cfglinks"
        link_dir.mkdir()
        os.symlink(avb, link_dir / "home")
        with open(proj / ".venv" / "pyvenv.cfg", "w") as f:
            f.write("home = cfglinks/home\n")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None, "pyvenv.cfg home resolving into app venv accepted"

    def test_b2_two_hop_interpreter_chain_refused(self, tmp_path, monkeypatch):
        """BUG#2: bin/python -> <app>/.venv/bin/python -> /usr/bin/python3.
        Final realpath /usr/bin/python3 is OUTSIDE the app venv; the dead
        clause-2 never fired here. The single-hop chain walk catches the
        intermediate <app>/.venv/bin/python hop → refused."""
        _, avb, _ = self._setup_app(tmp_path, monkeypatch)
        proj = self._foreign(tmp_path, "f2")
        os.remove(proj / ".venv" / "bin" / "python")
        os.symlink(avb / "python", proj / ".venv" / "bin" / "python")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None, (
            "two-hop interpreter chain through the app venv accepted"
        )

    def test_b2_realpath_collapse_documented_by_chain(self, tmp_path, monkeypatch):
        """Pins WHY the hop walk exists: realpath of the two-hop chain is
        /usr/bin/python3 (outside the app venv) — a containment check on
        the FINAL realpath alone cannot see the intermediate claim."""
        app, avb, _ = self._setup_app(tmp_path, monkeypatch)
        proj = self._foreign(tmp_path, "f2b")
        os.remove(proj / ".venv" / "bin" / "python")
        os.symlink(avb / "python", proj / ".venv" / "bin" / "python")
        from agent.enforcement import _symlink_chain
        python_path = str(proj / ".venv" / "bin" / "python")
        chain, truncated = _symlink_chain(python_path)
        assert truncated is False, "two-hop chain must not trip the cap"
        assert any(hop == str(avb / "python") for hop in chain), (
            f"hop walk missed the intermediate app-venv hop: {chain}"
        )
        # The final realpath lands OUTSIDE the app venv (system python).
        # Not a hard literal — /usr/bin/python3 is itself a symlink here.
        assert not os.path.realpath(python_path).startswith(
            str((app / ".venv").resolve())
        ), (
            "fixture assumption broken: final realpath is inside the app venv"
        )

    def test_clean_foreign_venv_no_claims_control(self, tmp_path, monkeypatch):
        """Control: a fully copied foreign venv (no symlinks into the app)
        → no claims, its python is used (venv-first contract intact)."""
        _, _, _ = self._setup_app(tmp_path, monkeypatch)
        proj = self._foreign(tmp_path, "f4")
        os.makedirs(proj / ".venv" / "lib" / "python3.12" / "site-packages")
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result == str(proj / ".venv" / "bin" / "python")
        from agent.enforcement import _venv_app_claims
        assert _venv_app_claims(str(proj), ".venv") == []

    def test_app_worktree_symlinked_venv_still_allowed(self, tmp_path, monkeypatch):
        """BUG#3 companion control: an app WORKTREE with .venv symlinked to
        the app venv stays legitimate (project_is_app via _is_app_worktree
        short-circuits the claim scan)."""
        app, _, _ = self._setup_app(tmp_path, monkeypatch)
        wt = app / ".worktrees" / "agent"
        wt.mkdir(parents=True)
        os.symlink(app / ".venv", wt / ".venv")
        result = _detect_venv_prefix(str(wt), ".venv")
        assert result == str(wt / ".venv" / "bin" / "python")


class TestV1ResolvedBinaryGate:
    """SP0 V1 — resolved-binary root allowlist (PATH-bleed).

    Shim-refused repro through the REAL check() path (marker must stay
    untouched), plus controls: clean PATH runs, `.` and user dirs in PATH
    are refused, project venv bin is allowed.
    """

    def _shim(self, directory: str, name: str, marker: str) -> str:
        os.makedirs(directory, exist_ok=True)
        shim = os.path.join(directory, name)
        with open(shim, "w") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\necho 'SHIM RAN args='$@\nexit 0\n")
        os.chmod(shim, 0o755)
        return shim

    def _victim_project(self, tmp_path) -> str:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "tests").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "x.py").write_text("x = 1\n")
        return str(proj)

    def _run_check(self, proj: str, monkeypatch):
        from agent.enforcement import _ENFORCEMENT_CONFIG_CACHE, _TEST_CONFIG_CACHE
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()
        with open(os.path.join(proj, ".crabcakes", "enforcement.json"), "w") as f:
            json.dump({"test": {
                "command": "python3 -m pytest {test_file} -q",
                "full_suite_command": "pytest tests/",
                "run_full_suite": True,
            }}, f)
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")},
        )
        return check(
            "write_file", {"path": "x.py"},
            ToolResult(success=True, output="", error="", duration_ms=1,
                       stdout="", stderr="", exit_code=0),
            proj,
            _make_config(test_run=True, lint_check=False),
        )

    def test_shim_refused_marker_untouched(self, tmp_path, monkeypatch):
        """THE repro: shim pytest FIRST on PATH → REFUSED, marker untouched.

        Reproduces the pre-fix failure (marker WAS touched, tier
        false-PASSED) and confirms it no longer happens.
        """
        shim_dir = str(tmp_path / "shim_bin")
        marker = str(tmp_path / "MARKER")
        self._shim(shim_dir, "pytest", marker)
        proj = self._victim_project(tmp_path)
        os.makedirs(os.path.join(proj, ".crabcakes"), exist_ok=True)
        # Stale-test repair (2026-11-19): the autouse _isolate_config_resolution
        # fixture (fa623cd6, 2026-10-11) pins HOME to tmp_path for EVERY test —
        # which put the planted shim INSIDE the trusted HOME root (BUG#4 round-2
        # ruling: the operator's home subtree is trusted tooling territory) and
        # the gate correctly admitted it. The V1 threat this test pins is a PATH
        # dir OUTSIDE the operator's home, so HOME gets a sibling fake home the
        # shim does not live under. Production behavior is unchanged.
        fake_home = tmp_path / "operator_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", shim_dir + os.pathsep + os.environ["PATH"])
        result = self._run_check(proj, monkeypatch)
        assert not os.path.exists(marker), "shim EXECUTED — PATH-bleed open"
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1
        assert tests_checks[0].passed is False
        assert "REFUSED" in tests_checks[0].detail
        assert "PATH shadowing" in tests_checks[0].detail

    def test_app_venv_bin_on_path_foreign_refused(self, tmp_path, monkeypatch):
        """SP0 fix round probe (a) (Debugger BUG#3): foreign project, NO venv,
        the APP's venv bin first on PATH → the tests tier must be a visible
        FAILED (REFUSED), never a run of the app's dependency set.

        Pre-fix: venv probe misses → bare `python3` → PATH resolves it into
        the app venv → realpath /usr/bin/python3 → admitted via a system
        root → `import nh3` false-PASSED against develcakes' site-packages.
        The gate now refuses any argv[0] resolving into the app venv for a
        non-app project. (Supersedes the old test_clean_path_control_runs,
        which pinned the opposite — and wrong — belief that the app
        interpreter dir is a sanctioned root for foreign projects.)
        """
        proj = self._victim_project(tmp_path)
        os.makedirs(os.path.join(proj, ".crabcakes"), exist_ok=True)
        # The victim test imports nh3 — present ONLY in the app venv.
        with open(os.path.join(proj, "tests", "test_x.py"), "w") as f:
            f.write("import nh3\n\ndef test_host_dep():\n    assert nh3 is not None\n")
        app_venv_bin = os.path.join(_APP_ROOT, ".venv", "bin")
        assert os.path.isdir(app_venv_bin), "fixture requires the app venv"
        monkeypatch.setenv("PATH", app_venv_bin + os.pathsep + os.environ["PATH"])
        result = self._run_check(proj, monkeypatch)
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1
        assert tests_checks[0].passed is False, (
            f"foreign project ran the app's dependency set — env bleed: "
            f"{tests_checks[0].detail}"
        )
        assert "REFUSED" in tests_checks[0].detail
        assert "app environment" in tests_checks[0].detail

    def test_dot_in_path_refused(self, tmp_path, monkeypatch):
        """`.` in PATH + shim in the current dir → refused (the `.` vector).

        The base PATH is kept AFTER `.` so the syntax tier still resolves
        python3 — the test targets the tests tier's refusal of the shim,
        not a whole-env starvation.
        """
        shim_dir = str(tmp_path / "dot_shim")
        marker = str(tmp_path / "MARKER3")
        self._shim(shim_dir, "pytest", marker)
        proj = self._victim_project(tmp_path)
        os.makedirs(os.path.join(proj, ".crabcakes"), exist_ok=True)
        # Stale-test repair (2026-11-19), same as the sibling shim test: the
        # autouse HOME-isolation fixture put the whole tmp tree inside the
        # trusted HOME root, so `.`-resolved ./pytest was admitted. HOME gets
        # a sibling fake home so the cwd shim stays OUTSIDE the trusted root —
        # the `.`-vector this test pins. Production behavior is unchanged.
        fake_home = tmp_path / "operator_home3"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        # PATH with `.` FIRST and cwd = the shim dir when the gate runs —
        # `which` resolves `pytest` → ./pytest → the shim → REFUSED.
        monkeypatch.setenv("PATH", "." + os.pathsep + os.environ["PATH"])
        monkeypatch.chdir(shim_dir)
        result = self._run_check(proj, monkeypatch)
        assert not os.path.exists(marker), "shim via `.` PATH EXECUTED"
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1
        assert tests_checks[0].passed is False
        assert "REFUSED" in tests_checks[0].detail

    def test_project_venv_bin_in_path_allowed(self, tmp_path, monkeypatch):
        """PATH containing the PROJECT's venv bin → allowed via root (c)."""
        proj = self._victim_project(tmp_path)
        os.makedirs(os.path.join(proj, ".crabcakes"), exist_ok=True)
        venv_bin = os.path.join(proj, ".venv", "bin")
        marker = str(tmp_path / "MARKER4")
        self._shim(venv_bin, "pytest", marker)
        # NOTE: this shim DOES execute (marker touched) — root (c) is a
        # DELIBERATE trust decision: the checked project's own venv is the
        # sanctioned location for project-supplied test commands. The test
        # asserts the GATE passes (no REFUSED), not that the binary is
        # benign; the marker is just proof the venv binary ran.
        # PATH with the project venv bin FIRST (shim shadows) + base PATH
        # after (python3 must still resolve for the syntax tier — a
        # venv-bin-only PATH rightly refuses it; the gate is per-binary).
        monkeypatch.setenv("PATH", venv_bin + os.pathsep + os.environ["PATH"])
        result = self._run_check(proj, monkeypatch)
        assert os.path.exists(marker), "project venv bin not honored"
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1
        assert "REFUSED" not in tests_checks[0].detail

    def test_running_interpreter_dir_allowed_for_app_only(self, tmp_path, monkeypatch):
        """Root (b) identity gate (Debugger BUG#3): the running interpreter's
        dir is a trusted root ONLY when the checked project IS the app.

        For the app project: gate passes (the interpreter dir admits
        sys.executable; on the dev layout its realpath additionally lands
        in a system bin). For a FOREIGN project: the same argv is refused —
        substituting develcakes' interpreter dir into a foreign project's
        roots is the env-bleed hole probe (a) drove through (refused venv →
        bare python3 → PATH-resolved app-venv python → admitted → the app's
        dependency set ran the tests)."""
        from agent.enforcement import _ALLOWED_BINARY_ROOTS, _validate_resolved_binary
        argv = [sys.executable, "-c", "pass"]
        allowed, detail = _validate_resolved_binary(argv, _APP_ROOT)
        assert allowed, f"app project must admit its own interpreter: {detail}"
        real_exe = os.path.realpath(sys.executable)
        in_system = any(
            real_exe == r or real_exe.startswith(r + os.sep)
            for r in _ALLOWED_BINARY_ROOTS
        )
        assert in_system or real_exe.startswith(
            os.path.realpath(os.path.dirname(sys.executable))
        )
        foreign = str(tmp_path / "foreign")
        os.makedirs(foreign)
        allowed, detail = _validate_resolved_binary(argv, foreign)
        assert not allowed, (
            "foreign project admitted the app interpreter dir — identity "
            f"gate missing: {detail}"
        )
        assert "app environment" in detail or "outside allowed roots" in detail

    def test_which_miss_fail_closed(self, tmp_path, monkeypatch):
        """Binary missing from the scrubbed PATH entirely → refused
        (fail-closed), with the token named."""
        from agent.enforcement import _validate_resolved_binary
        monkeypatch.setenv("PATH", str(tmp_path))  # empty dir on PATH
        allowed, detail = _validate_resolved_binary(
            ["definitely_missing_binary_xyz"], str(tmp_path)
        )
        assert allowed is False
        assert "not found" in detail
        assert "definitely_missing_binary_xyz" in detail


class TestSP0FixRound:
    """SPEC-09 SP0 fix round (Debugger audit 2026-10-02) — BUG#3(b), #5, #6.

    Each test reproduces its audit probe through the REAL check() path
    where the probe did, not just the helper.
    """

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _project(self, tmp_path, with_nh3_test=True):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "tests").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "x.py").write_text("x = 1\n")
        if with_nh3_test:
            (proj / "tests" / "test_x.py").write_text(
                "import nh3\n\ndef test_host_dep():\n    assert nh3 is not None\n"
            )
        os.makedirs(proj / ".crabcakes")
        return str(proj)

    def _write_cfg(self, proj, test_section):
        with open(os.path.join(proj, ".crabcakes", "enforcement.json"), "w") as f:
            json.dump({"test": test_section}, f)
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _shim(self, directory: str, name: str, marker: str) -> str:
        os.makedirs(directory, exist_ok=True)
        shim = os.path.join(directory, name)
        with open(shim, "w") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        os.chmod(shim, 0o755)
        return shim

    def _check(self, proj, monkeypatch, lint=False):
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")},
        )
        return check(
            "write_file", {"path": "x.py"},
            ToolResult(success=True, output="", error="", duration_ms=1,
                       stdout="", stderr="", exit_code=0),
            proj,
            _make_config(test_run=True, lint_check=lint),
        )

    # ── BUG#3 probe (b): refused venv must be a visible FAILED tier ──

    def test_b3_refused_venv_dir_symlink_failed_tier_not_path_run(self, tmp_path, monkeypatch):
        """BUG#3 probe (b): foreign project whose .venv SYMLINKS to the app
        venv (refused by the claim scan) + the app venv bin NOT on PATH —
        the refusal must be a visible FAILED tier naming the surface, NOT a
        PATH-resolved bare-python3 run. (On hosts where PATH-resolved python3
        has pytest — e.g. the app venv on PATH — the pre-fix behavior was a
        false PASS of the foreign test via the app dependency set.)"""
        proj = self._project(tmp_path)
        os.symlink(_APP_ROOT + "/.venv", os.path.join(proj, ".venv"))
        self._write_cfg(proj, {
            "command": "python3 -m pytest {test_file} -q",
            "venv_path": ".venv",
        })
        # PATH WITHOUT the app venv (system python3 resolves elsewhere).
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        result = self._check(proj, monkeypatch)
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1, (
            f"tests tier must produce a visible check, got {result.checks}"
        )
        assert tests_checks[0].passed is False
        assert "test interpreter refused (venv validation)" in tests_checks[0].detail
        assert "site-packages" in tests_checks[0].detail or "venv dir" in tests_checks[0].detail

    def test_b3_refused_venv_app_bin_on_path_still_failed(self, tmp_path, monkeypatch):
        """BUG#3 probe (a)+ gate rule: refused venv + the APP's venv bin on
        PATH → FAILED, never a run of the app's dependency set (the nh3
        import would false-PASS via develcakes' site-packages)."""
        proj = self._project(tmp_path)
        os.symlink(_APP_ROOT + "/.venv", os.path.join(proj, ".venv"))
        self._write_cfg(proj, {
            "command": "python3 -m pytest {test_file} -q",
            "venv_path": ".venv",
            "run_full_suite": True,
            "full_suite_command": "pytest tests/",
        })
        monkeypatch.setenv(
            "PATH", os.path.join(_APP_ROOT, ".venv", "bin") + os.pathsep + "/usr/bin:/bin"
        )
        result = self._check(proj, monkeypatch)
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1
        assert tests_checks[0].passed is False
        assert "REFUSED" in tests_checks[0].detail or "refused" in tests_checks[0].detail

    # ── BUG#5: refused syntax is gate-neutral for tests/lint ──

    def test_b5_refused_syntax_cascade_dead(self, tmp_path, monkeypatch):
        """BUG#5 cascade probe: syntax tier REFUSED (untrusted python3
        shape) → the tests tier must STILL run (a visible check), not be
        silently disabled by the pre-fix gating rule. The syntax refusal is
        gate-neutral, exactly like SKIPPED."""
        proj = self._project(tmp_path)
        self._write_cfg(proj, {
            "command": "python3 -m pytest {test_file} -q",
            "venv_path": ".venv",
        })
        venv_dir = os.path.join(proj, ".venv")
        # .venv does not exist yet (_project creates only tests/ and
        # .crabcakes/) — plant the symlink directly.
        os.symlink(_APP_ROOT + "/.venv", venv_dir)
        _TEST_CONFIG_CACHE.clear()
        # Syntax tier: `python3` resolves into the app venv (foreign proj
        # + app-venv PATH) → REFUSED by the BUG#3 gate rule. With the
        # pre-fix gating, this refusal cascaded and killed the tests tier.
        monkeypatch.setenv(
            "PATH", os.path.join(_APP_ROOT, ".venv", "bin") + os.pathsep + "/usr/bin:/bin"
        )
        result = self._check(proj, monkeypatch)
        tiers = [c.tier for c in result.checks]
        assert "syntax" in tiers
        syntax_check = next(c for c in result.checks if c.tier == "syntax")
        assert syntax_check.passed is False
        assert "REFUSED" in syntax_check.detail
        assert "tests" in tiers, (
            f"BUG#5 cascade: refused syntax disabled the tests tier — "
            f"tiers present: {tiers}"
        )

    # ── BUG#6: `command` field through the token allowlist ──

    def test_b6_command_field_sh_c_refused_marker_untouched(self, tmp_path, monkeypatch):
        """BUG#6: enforcement.json test.command = `sh -c 'touch MARKER'` —
        pre-fix the branch parsed and EXECUTED it (shell=False still runs
        argv[0]='sh'; /usr/bin/sh is an admitted system binary). The token
        allowlist must refuse it; the marker stays untouched."""
        import time as _time
        proj = self._project(tmp_path)
        marker = str(tmp_path / "MARKER6")
        self._write_cfg(proj, {
            "command": f"sh -c 'touch {marker}'",
            "venv_path": ".venv",
        })
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        result = self._check(proj, monkeypatch)
        _time.sleep(0.15)
        assert not os.path.exists(marker), (
            f"sh -c EXECUTED — command field bypassed the token allowlist; "
            f"checks: {result.checks}"
        )
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        # C-C (PM RULING 2026-11-23) made a refused `test.command` a VISIBLE
        # FAILED tier instead of a silent `return None` skip, so this probe can
        # now assert the check EXISTS — the pre-C-C statement was the lenient
        # `not tests_checks or ...`, which a silent skip also satisfied.
        assert len(tests_checks) == 1, (
            f"token refusal SKIPPED the tests tier instead of failing it: "
            f"{result.checks}"
        )
        assert tests_checks[0].passed is False
        assert "REFUSED" in tests_checks[0].detail, tests_checks[0].detail

    # ── BUG#4: HOME in trusted roots (fake-HOME trio) ──────────────────

    def test_b4_linter_under_fake_home_runs(self, tmp_path, monkeypatch):
        """BUG#4 ruling: binaries under the operator's HOME are trusted.

        SP0 fix round 2 rebuild: the PROJECT sits under the fake HOME
        (the real ~/projects/<repo> layout) while the tool lives at the
        HOME level (``<fakeHOME>/.local/bin``), OUTSIDE the project —
        the round-1 fixture planted the tool INSIDE the project dir
        (tmp_path doubled as project root), which encoded the false
        invariant BUG#10 has now closed (project-containment overrides
        the HOME root)."""
        from agent.enforcement import _validate_resolved_binary
        fake_home = tmp_path / "fakehome"
        proj = fake_home / "projects" / "myrepo"
        proj.mkdir(parents=True)
        tool_dir = fake_home / ".local" / "bin"
        tool_dir.mkdir(parents=True)
        self._shim(str(tool_dir), "ruff", str(tmp_path / "unused_marker"))
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": str(tool_dir) + os.pathsep + "/usr/bin:/bin",
                     "HOME": str(fake_home)},
        )
        allowed, detail = _validate_resolved_binary(["ruff", "check", "x.py"], str(proj))
        assert allowed, f"HOME-rooted tooling refused — ruling violated: {detail}"

    def test_b4_tmp_binary_refused(self, tmp_path, monkeypatch):
        """Control: the same binary shape in /tmp (NOT under HOME, not a
        system bin, not a project venv) → refused (the V1 shim catch
        stays closed under the new ruling)."""
        from agent.enforcement import _validate_resolved_binary
        tmp_bin = tmp_path / "tmpbin"
        tmp_bin.mkdir()
        self._shim(str(tmp_bin), "ruff", str(tmp_path / "unused_marker2"))
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": str(tmp_bin) + os.pathsep + "/usr/bin:/bin",
                     "HOME": os.environ.get("HOME", "/root")},
        )
        allowed, detail = _validate_resolved_binary(["ruff", "check", "x.py"], str(tmp_path))
        assert not allowed, "/tmp binary admitted — trust boundary broken"
        # The project here IS tmp_path, so the BUG#10 project-containment
        # rule fires first (the shim dir is inside the project); either
        # refusal detail proves the boundary holds.
        assert (
            "inside the checked project" in detail or "outside allowed roots" in detail
        ), detail

    def test_b4_project_dir_outside_venv_bin_refused(self, tmp_path, monkeypatch):
        """Control: binary in the PROJECT dir but OUTSIDE the venv bin
        (e.g. <proj>/tools/ruff) → refused by the BUG#10 project-
        containment override, with the project under a fake HOME (the
        round-1 /tmp-only placement encoded the false invariant that only
        /tmp was the threat; under HOME the same project-local binary was
        admitted via the trusted-HOME root)."""
        from agent.enforcement import _validate_resolved_binary
        fake_home = tmp_path / "fakehome"
        proj = fake_home / "projects" / "myrepo"
        tools_dir = proj / "tools"
        tools_dir.mkdir(parents=True)
        self._shim(str(tools_dir), "ruff", str(tmp_path / "unused_marker3"))
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": str(tools_dir) + os.pathsep + "/usr/bin:/bin",
                     "HOME": str(fake_home)},
        )
        allowed, detail = _validate_resolved_binary(["ruff", "check", "x.py"], str(proj))
        assert not allowed, "project-dir (non-venv-bin) binary admitted via HOME root"
        assert "inside the checked project" in detail



class TestSP0FixRound2:
    """SPEC-09 SP0 fix round 2 (Debugger re-audit 2026-10-02) — BUG#9-#14.

    Two HIGH false-PASS classes closed: BUG#9 (decoy site-packages evades
    the single-candidate scan) and BUG#10 (trusted-HOME root admits
    project-local binaries for the ~/projects/<repo> layout). Each probe
    runs through the REAL check()/gate path, not just the helper.
    """

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _setup_app(self, tmp_path, monkeypatch):
        from agent import enforcement
        app = tmp_path / "app"
        avb = app / ".venv" / "bin"
        avb.mkdir(parents=True)
        os.symlink("/usr/bin/python3", avb / "python")
        app_sp = app / ".venv" / "lib" / "python3.12" / "site-packages"
        app_sp.mkdir(parents=True)
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(app))
        return app, avb, app_sp

    def _shim(self, directory, name, marker):
        os.makedirs(directory, exist_ok=True)
        shim = os.path.join(directory, name)
        with open(shim, "w") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        os.chmod(shim, 0o755)
        return shim

    def _victim_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "tests").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "x.py").write_text("x = 1\n")
        (proj / "tests" / "test_x.py").write_text(
            "import nh3\n\ndef test_host_dep():\n    assert nh3 is not None\n"
        )
        os.makedirs(proj / ".crabcakes")
        return str(proj)

    def _write_cfg(self, proj, test_section):
        with open(os.path.join(proj, ".crabcakes", "enforcement.json"), "w") as f:
            json.dump({"test": test_section}, f)
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _check(self, proj, monkeypatch):
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")},
        )
        return check(
            "write_file", {"path": "x.py"},
            ToolResult(success=True, output="", error="", duration_ms=1,
                       stdout="", stderr="", exit_code=0),
            proj,
            _make_config(test_run=True, lint_check=False),
        )

    # ── BUG#9 + #11: multi-candidate site-packages scan ──

    def test_b9_decoy_site_packages_refused(self, tmp_path, monkeypatch):
        """BUG#9 auditor probe: REAL python -m venv, decoy lib/python3.10/
        site-packages (sorts FIRST) + a pyvenv.cfg LYING about version
        (3.10) while the REAL app-linked site-packages sits under
        python3.12 → the scan must check BOTH candidates and refuse on
        the real one. Pre-fix: single-candidate early-return admitted the
        venv → `import nh3` false-PASSED."""
        _, _, app_sp = self._setup_app(tmp_path, monkeypatch)
        import subprocess as _sp
        real_venv = tmp_path / "realvenv"
        _sp.run(
            ["/usr/bin/python3", "-m", "venv", "--without-pip", str(real_venv)],
            check=True, capture_output=True,
        )
        # The REAL venv's site-packages → the app's (the dependency steal).
        real_sp_src = real_venv / "lib" / "python3.12" / "site-packages"
        assert real_sp_src.is_dir(), "fixture: real venv layout changed"
        os.rmdir(real_sp_src)
        os.symlink(app_sp, real_sp_src)
        # The DECOY: a lower-version dir that sorts before the real one.
        decoy = real_venv / "lib" / "python3.10" / "site-packages"
        decoy.mkdir(parents=True)
        # The LIE: pyvenv.cfg claims version 3.10 (the decoy's version).
        with open(real_venv / "pyvenv.cfg", "r") as f:
            cfg = f.read()
        with open(real_venv / "pyvenv.cfg", "w") as f:
            f.write(cfg.replace("version = 3.12", "version = 3.10"))
        proj = self._victim_project(tmp_path)
        os.symlink(real_venv, os.path.join(proj, ".venv"))
        self._write_cfg(proj, {
            "command": "python3 -m pytest {test_file} -q",
            "venv_path": ".venv",
        })
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        result = self._check(proj, monkeypatch)
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1, f"missing tests tier check: {result.checks}"
        assert tests_checks[0].passed is False
        assert "test interpreter refused (venv validation)" in tests_checks[0].detail
        assert "site-packages" in tests_checks[0].detail

    def test_b11_lib64_site_packages_refused(self, tmp_path, monkeypatch):
        """BUG#11: a venv-shaped dir whose ONLY site-packages lives under
        lib64/ (linked into the app) → refused (the round-1 glob only
        covered lib/)."""
        _, _, app_sp = self._setup_app(tmp_path, monkeypatch)
        proj = tmp_path / "f11"
        bin_dir = proj / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        os.symlink("/usr/bin/python3", bin_dir / "python")
        os.makedirs(proj / ".venv" / "lib64" / "python3.12")
        os.symlink(
            app_sp, proj / ".venv" / "lib64" / "python3.12" / "site-packages"
        )
        from agent.enforcement import _venv_site_packages_all
        all_sp = _venv_site_packages_all(str(proj / ".venv"))
        assert any(str(app_sp) == sp for sp in all_sp), (
            f"lib64 candidate not enumerated: {all_sp}"
        )
        result = _detect_venv_prefix(str(proj), ".venv")
        assert result is None, "lib64-linked site-packages admitted"

    def test_b9_multi_candidate_control_clean_venv_runs(self, tmp_path, monkeypatch):
        """Control: a real venv with TWO local site-packages dirs (decoy +
        real, neither linked into the app) → no claim, python used."""
        _, _, _ = self._setup_app(tmp_path, monkeypatch)
        import subprocess as _sp
        real_venv = tmp_path / "realvenv2"
        _sp.run(
            ["/usr/bin/python3", "-m", "venv", "--without-pip", str(real_venv)],
            check=True, capture_output=True,
        )
        decoy = real_venv / "lib" / "python3.10" / "site-packages"
        decoy.mkdir(parents=True)
        proj = self._victim_project(tmp_path)
        os.symlink(real_venv, os.path.join(proj, ".venv"))
        self._write_cfg(proj, {
            "command": "python3 -m pytest {test_file} -q",
            "venv_path": ".venv",
        })
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        result = self._check(proj, monkeypatch)
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1
        # Not refused by the venv gate — it ran (and failed on nh3 missing
        # or passed; either is the tier RUNNING, not the venv gate).
        assert "test interpreter refused (venv validation)" not in tests_checks[0].detail

    # ── BUG#10: project-containment overrides trust roots ──

    def test_b10_project_local_binary_refused_under_home(self, tmp_path, monkeypatch):
        """BUG#10 auditor probe: project under a FAKE HOME (the
        ~/projects/<repo> layout), <proj>/tools/pytest supplied via
        enforcement.json command → REFUSED by the project-containment
        override, marker untouched. Pre-fix: admitted via the trusted
        HOME root (the probe's own repo shape)."""
        fake_home = tmp_path / "fakehome"
        proj = fake_home / "projects" / "myrepo"
        (proj / "tests").mkdir(parents=True)
        (proj / ".crabcakes").mkdir(parents=True)
        (proj / "x.py").write_text("x = 1\n")
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "tests" / "test_x.py").write_text("def test_ok(): assert True\n")
        tools_bin = proj / "tools"
        marker = str(tmp_path / "MARKER10")
        self._shim(str(tools_bin), "pytest", marker)
        # C-B-1: no positional here. A `pytest tests/test_x.py` command is now
        # refused by the argv grammar BEFORE the resolved-binary gate, which
        # would make this test green for the wrong reason — it pins the
        # containment rule, so its command must reach that gate.
        self._write_cfg(str(proj), {
            "command": "pytest -q",
            "venv_path": ".venv",
        })
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv(
            "PATH", str(tools_bin) + os.pathsep + "/usr/bin:/bin"
        )
        result = self._check(str(proj), monkeypatch)
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1, f"missing tests tier: {result.checks}"
        assert tests_checks[0].passed is False
        assert "inside the checked project" in tests_checks[0].detail
        assert not os.path.exists(marker), "project-local binary EXECUTED"

    def test_b10_project_venv_bin_still_admitted_control(self, tmp_path, monkeypatch):
        """Control: <proj>/.venv/bin/pytest (root (c)) → still admitted —
        the venv-bin carve-out in the project-containment rule holds."""
        from agent.enforcement import _validate_resolved_binary
        fake_home = tmp_path / "fakehome"
        proj = fake_home / "projects" / "myrepo"
        venv_bin = proj / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        self._shim(str(venv_bin), "pytest", str(tmp_path / "MARKER_CTRL"))
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": str(venv_bin) + os.pathsep + "/usr/bin:/bin",
                     "HOME": str(fake_home)},
        )
        allowed, detail = _validate_resolved_binary(
            ["pytest", "tests/"], str(proj)
        )
        assert allowed, f"project venv bin carve-out broken: {detail}"

    # ── BUG#12: truthful conservative refusal detail ──

    def test_b12_mirrored_interpreter_still_refused_truthful_detail(self, tmp_path, monkeypatch):
        """BUG#12 pin: foreign venv with its OWN cfg + OWN site-packages
        (genuinely local except the binary) whose bin/python links through
        the app venv's python → STILL refused (conservative behavior
        pinned so a future loosening is deliberate), with the truthful
        'conservative refusal' wording."""
        _, avb, _ = self._setup_app(tmp_path, monkeypatch)
        proj = tmp_path / "f12"
        bin_dir = proj / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        os.symlink(avb / "python", bin_dir / "python")
        with open(proj / ".venv" / "pyvenv.cfg", "w") as f:
            f.write("home = /usr/bin\nversion = 3.12.3\n")
        own_sp = proj / ".venv" / "lib" / "python3.12" / "site-packages"
        own_sp.mkdir(parents=True)
        from agent.enforcement import _venv_refusal_reason
        reason = _venv_refusal_reason(str(proj), ".venv")
        assert reason is not None, "mirrored-interpreter venv admitted — pin broken"
        assert "conservative refusal" in reason
        assert "claims the app environment" not in reason

    # ── BUG#13: cap truncation is a claim ──

    def test_b13_41_hop_chain_refused(self, tmp_path, monkeypatch):
        """BUG#13: a 41-hop symlink chain → the walk truncates at the cap
        and the truncation itself is a CLAIM → refused with the
        fail-closed reason (probe_sp0fixround10's CAP shape)."""
        _, _, _ = self._setup_app(tmp_path, monkeypatch)
        proj = tmp_path / "f13"
        bin_dir = proj / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        # Build a 41-hop chain: hop_40 -> hop_39 -> ... -> hop_0 -> real.
        link = bin_dir / "python"
        target = "/usr/bin/python3"
        for i in range(41):
            next_link = bin_dir / f"hop_{i}"
            os.symlink(target, next_link)
            target = str(next_link)
        os.symlink(target, link)
        from agent.enforcement import _symlink_chain, _venv_refusal_reason
        _hops, truncated = _symlink_chain(str(link))
        assert truncated is True, "41-hop chain must trip the cap"
        reason = _venv_refusal_reason(str(proj), ".venv")
        assert reason is not None, "truncated chain admitted — fail-closed broken"
        assert "exceeded 40 hops" in reason


class TestSP0FixRound3:
    """SPEC-09 SP0 fix round 3 (Debugger re-audit 2026-10-02) — BUG#15-#17.

    BUG#15 (HIGH): glob.glob treats the project-supplied venv path as a
    PATTERN — ``venv_path=".venv[1]"`` opens a character class, the
    site-packages scan returns [] and an app-linked site-packages goes
    unseen (auditor probe: tier false-PASSED with ``import nh3``). The fix
    escapes the literal base at BOTH glob sites. BUG#16 (MEDIUM): the
    app-env refusal compared a LITERAL dirname against the realpath'd app
    venv while root (c) was realpath'd — a foreign ``.venv`` symlinked to
    the app venv got the app venv bin as a trusted root (auditor e2e: the
    foreign syntax tier EXECUTED a planted app-venv binary). The fix
    normalizes BOTH sides and requires root (c) to sit literally inside the
    checked project. BUG#17: stale docstring (no test — prose only).
    """

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _setup_app(self, tmp_path, monkeypatch):
        from agent import enforcement
        app = tmp_path / "app"
        avb = app / ".venv" / "bin"
        avb.mkdir(parents=True)
        os.symlink("/usr/bin/python3", avb / "python")
        app_sp = app / ".venv" / "lib" / "python3.12" / "site-packages"
        app_sp.mkdir(parents=True)
        monkeypatch.setattr(enforcement, "_APP_ROOT", str(app))
        return app, avb, app_sp

    def _victim_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "tests").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "x.py").write_text("x = 1\n")
        (proj / "tests" / "test_x.py").write_text(
            "import nh3\n\ndef test_host_dep():\n    assert nh3 is not None\n"
        )
        os.makedirs(proj / ".crabcakes")
        return str(proj)

    def _write_cfg(self, proj, test_section):
        with open(os.path.join(proj, ".crabcakes", "enforcement.json"), "w") as f:
            json.dump({"test": test_section}, f)
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _shim(self, directory, name, marker):
        os.makedirs(directory, exist_ok=True)
        shim = os.path.join(directory, name)
        with open(shim, "w") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        os.chmod(shim, 0o755)
        return shim

    # ── BUG#15: glob metacharacter evasion in the site-packages scan ──

    def test_b15_bracket_venv_glob_metachar_refused(self, tmp_path, monkeypatch):
        """BUG#15 auditor probe: foreign project whose enforcement.json
        carries ``venv_path: ".venv[1]"`` — ``[`` opens a glob character
        class, the unescaped scan returns [] and the app-linked
        site-packages goes UNSEEN → the venv is admitted (auditor's probe:
        tier false-PASSED with ``import nh3``). No pyvenv.cfg: the cfg
        ``version`` candidate is a literal join and would mask the glob
        hole — the glob is the ONLY surface that finds the link here.
        Wiring mirrors production: venv_path reaches _detect_venv_prefix
        from the config's test section via _check_tests."""
        _, _, app_sp = self._setup_app(tmp_path, monkeypatch)
        from agent.enforcement import _venv_app_claims
        proj = tmp_path / "f15"
        bin_dir = proj / ".venv[1]" / "bin"
        bin_dir.mkdir(parents=True)
        os.symlink("/usr/bin/python3", bin_dir / "python")
        lib_dir = proj / ".venv[1]" / "lib" / "python3.12"
        lib_dir.mkdir(parents=True)
        os.symlink(app_sp, lib_dir / "site-packages")
        result = _detect_venv_prefix(str(proj), ".venv[1]")
        assert result is None, (
            "metachar venv name blinded the site-packages scan — admitted: "
            f"{result}"
        )
        claims = _venv_app_claims(str(proj), ".venv[1]")
        assert any(surface == "site-packages" for surface, _ in claims), (
            f"claim must name site-packages, got: {claims}"
        )

    def test_b15_bracket_venv_e2e_tests_tier_refused(self, tmp_path, monkeypatch):
        """BUG#15 e2e through the REAL check() path: enforcement.json
        ``venv_path: ".venv[1]"`` → the tests tier must be a visible FAILED
        naming the site-packages claim. Pre-fix the refusal never fired and
        the tier RAN (system python3, PATH) — a FAILED run is NOT the
        refusal, hence the detail assertion, not just passed=False."""
        _, _, app_sp = self._setup_app(tmp_path, monkeypatch)
        proj = tmp_path / "f15e2e"
        bin_dir = proj / ".venv[1]" / "bin"
        bin_dir.mkdir(parents=True)
        os.symlink("/usr/bin/python3", bin_dir / "python")
        lib_dir = proj / ".venv[1]" / "lib" / "python3.12"
        lib_dir.mkdir(parents=True)
        os.symlink(app_sp, lib_dir / "site-packages")
        (proj / "tests").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "x.py").write_text("x = 1\n")
        (proj / "tests" / "test_x.py").write_text(
            "import nh3\n\ndef test_host_dep():\n    assert nh3 is not None\n"
        )
        os.makedirs(proj / ".crabcakes")
        self._write_cfg(str(proj), {
            "command": "python3 -m pytest {test_file} -q",
            "venv_path": ".venv[1]",
        })
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")},
        )
        result = check(
            "write_file", {"path": "x.py"},
            ToolResult(success=True, output="", error="", duration_ms=1,
                       stdout="", stderr="", exit_code=0),
            str(proj),
            _make_config(test_run=True, lint_check=False),
        )
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1, f"missing tests tier: {result.checks}"
        assert tests_checks[0].passed is False
        assert "test interpreter refused (venv validation)" in tests_checks[0].detail, (
            f"tier RAN instead of refusing (pre-fix shape): "
            f"{tests_checks[0].detail}"
        )
        assert "site-packages" in tests_checks[0].detail

    def test_b15_star_venv_glob_variant_refused(self, tmp_path, monkeypatch):
        """BUG#15 ``*`` variant (``.venv*old``) over lib64: the escaped scan
        must still FIND the app-linked site-packages. Pin note: on this
        Python ``*``/``?`` self-match the literal directory (a pattern
        component matches any run INCLUDING the metachar itself), so this
        variant pins the escape is applied at the lib64 site without
        breaking discovery — the ``[`` class above is the evasion that is
        RED pre-fix."""
        _, _, app_sp = self._setup_app(tmp_path, monkeypatch)
        proj = tmp_path / "f15star"
        bin_dir = proj / ".venv*old" / "bin"
        bin_dir.mkdir(parents=True)
        os.symlink("/usr/bin/python3", bin_dir / "python")
        lib_dir = proj / ".venv*old" / "lib64" / "python3.12"
        lib_dir.mkdir(parents=True)
        os.symlink(app_sp, lib_dir / "site-packages")
        from agent.enforcement import _venv_app_claims, _venv_site_packages_all
        all_sp = _venv_site_packages_all(str(proj / ".venv*old"))
        assert any(str(app_sp) == sp for sp in all_sp), (
            f"escaped scan lost a metachar-named venv's site-packages: {all_sp}"
        )
        result = _detect_venv_prefix(str(proj), ".venv*old")
        assert result is None, "star-venv app-linked site-packages admitted"
        claims = _venv_app_claims(str(proj), ".venv*old")
        assert any(surface == "site-packages" for surface, _ in claims)

    # ── BUG#16: literal-vs-realpath normalization asymmetry ──

    def test_b16_foreign_symlinked_venv_planted_app_binary_refused(
        self, tmp_path, monkeypatch
    ):
        """BUG#16 auditor e2e: foreign project with ``.venv → <app>/.venv``
        and ``<proj>/.venv/bin`` on PATH — the syntax tier's ``python3``
        resolves through the symlink to the planted ``<app>/.venv/bin/
        python3``. Pre-fix the app-env refusal compared the LITERAL dirname
        (outside the app venv) while root (c) was realpath'd (inside it) →
        the app binary EXECUTED (marker touched, tier false-PASSED). Fix:
        both sides realpath'd + root (c) must sit literally inside the
        project → REFUSED, marker untouched."""
        app, _avb, _app_sp = self._setup_app(tmp_path, monkeypatch)
        marker = str(tmp_path / "MARKER16")
        self._shim(str(app / ".venv" / "bin"), "python3", marker)
        proj = self._victim_project(tmp_path)
        # Replace the victim's .crabcakes-created default: no test section
        # needed — this probe rides the SYNTAX tier only.
        os.symlink(app / ".venv", os.path.join(proj, ".venv"))
        foreign_bin = os.path.join(proj, ".venv", "bin")
        monkeypatch.setenv(
            "PATH", foreign_bin + os.pathsep + "/usr/bin:/bin"
        )
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")},
        )
        result = check(
            "write_file", {"path": "x.py"},
            ToolResult(success=True, output="", error="", duration_ms=1,
                       stdout="", stderr="", exit_code=0),
            proj,
            _make_config(syntax_check=True, test_run=False, lint_check=False),
        )
        assert not os.path.exists(marker), (
            "planted app-venv binary EXECUTED by a foreign project — "
            f"normalization asymmetry open; checks: {result.checks}"
        )
        syntax_checks = [c for c in result.checks if c.tier == "syntax"]
        assert len(syntax_checks) == 1, f"missing syntax tier: {result.checks}"
        assert syntax_checks[0].passed is False
        assert "REFUSED" in syntax_checks[0].detail
        assert "app environment" in syntax_checks[0].detail

    def test_b16_root_c_requires_venv_literally_inside_project(
        self, tmp_path, monkeypatch
    ):
        """BUG#16 gate-level pin: the same indirection against argv[0]
        ``pytest`` — root (c) is realpath(proj/.venv/bin) = the APP venv
        bin for a symlinked foreign venv, which pre-fix ADMITTED the app
        binary. Post-fix root (c) is only a root when the (realpath'd)
        venv bin sits literally inside the checked project, so the
        resolution hits the realpath'd app-env refusal instead."""
        app, avb, _app_sp = self._setup_app(tmp_path, monkeypatch)
        self._shim(str(avb), "pytest", str(tmp_path / "MARKER16B"))
        proj = tmp_path / "f16gate"
        proj.mkdir()
        os.symlink(app / ".venv", proj / ".venv")
        from agent.enforcement import _validate_resolved_binary
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {
                "PATH": str(proj / ".venv" / "bin") + os.pathsep + "/usr/bin:/bin",
                "HOME": os.environ.get("HOME", ""),
            },
        )
        allowed, detail = _validate_resolved_binary(
            ["pytest", "tests/"], str(proj)
        )
        assert not allowed, (
            "foreign symlinked venv admitted the app binary via realpath'd "
            f"root (c): {detail}"
        )
        assert "app environment" in detail, detail

    def test_b16_sp2_worktree_symlinked_venv_still_allowed_gate_control(
        self, tmp_path, monkeypatch
    ):
        """SP2 control (probe_sp0fixround13 shape): an APP WORKTREE with
        ``.venv`` symlinked to the app venv stays ADMITTED at the gate —
        worktrees are identity-gated root (b) consumers (the running
        interpreter dir), not root (c) consumers, so requiring root (c) to
        sit inside the project does not touch the SP2 contract."""
        app, avb, _app_sp = self._setup_app(tmp_path, monkeypatch)
        self._shim(str(avb), "pytest", str(tmp_path / "MARKER16C"))
        # Make root (b) real for the fixture: the "running" interpreter is
        # the app venv's python.
        monkeypatch.setattr(
            "agent.enforcement.sys.executable", str(avb / "python")
        )
        wt = app / ".worktrees" / "agent"
        wt.mkdir(parents=True)
        os.symlink(app / ".venv", wt / ".venv")
        from agent.enforcement import _validate_resolved_binary
        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {
                "PATH": str(wt / ".venv" / "bin") + os.pathsep + "/usr/bin:/bin",
                "HOME": os.environ.get("HOME", ""),
            },
        )
        allowed, detail = _validate_resolved_binary(
            ["pytest", "tests/"], str(wt)
        )
        assert allowed, (
            "SP2 contract broken: app worktree + symlinked venv refused at "
            f"the gate — {detail}"
        )


# ── CONSOLIDATED audit Fix 3 (C2): the tier never runs project config ───────
#
# C2 was the strongest vector in the audit: a CLONED repository ships
# `pytest.ini` with `addopts = -p evilplugin` (and/or a root `conftest.py`),
# the tier ran `python3 -m pytest <file>` with cwd=project_path and no
# `-c`/`--confcutdir`, pytest honoured the project's ini, imported the
# plugin, executed it — and reported PASSED. No agent-authored file, no
# exec_command, no approval card.
#
# These tests run the REAL tier (via _check_tests / check) against a project
# whose venv python execs the running interpreter, so a marker that appears
# means something genuinely executed. Marker ABSENT is the closed state.


class TestConsolidatedFix3TierIsolation:
    """C2: project-supplied pytest config cannot reach the tier."""

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _project(self, tmp_path, *, ini=None, conftest=None,
                 test_conftest=None, plugin=True, marker_name="MARKER"):
        """A foreign project with a real venv-backed tier and a shipped payload."""
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / ".crabcakes").mkdir()
        (proj / "feature.py").write_text("x = 1\n")
        (proj / "tests" / "test_feature.py").write_text(
            "def test_ok():\n    assert True\n")
        marker = tmp_path / marker_name
        if ini is not None:
            (proj / "pytest.ini").write_text(ini)
        if plugin:
            # `-p evilplugin` resolves because pytest puts the rootdir (and a
            # root conftest's dir) on sys.path — probe M1b.
            (proj / "evilplugin.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('plugin ran')\n")
        if conftest is not None:
            (proj / "conftest.py").write_text(conftest)
        if test_conftest is not None:
            (proj / "tests" / "conftest.py").write_text(test_conftest)
        _create_real_venv(str(proj))
        _write_enforcement_json(str(proj), {
            "test": {
                "command": "python3 -m pytest {test_file} -v",
                "venv_path": ".venv",
            }
        })
        return str(proj), marker

    def test_shipped_pytest_ini_addopts_plugin_does_not_execute(self, tmp_path):
        """The audit's headline C2 probe (M1): `addopts = -p evilplugin`.

        Pre-fix: marker exists and the tier reports PASSED. Post-fix: the
        tier still runs and reports a real result, and the marker does NOT
        exist — the project's ini was never read.
        """
        proj, marker = self._project(
            tmp_path,
            ini="[pytest]\naddopts = -p evilplugin\n",
        )
        config = _make_config()
        result = _check_tests("feature.py", proj, config, syntax_passed=True)
        assert result is not None, "the tier must still run (isolation, not disable)"
        assert result.passed is True, result.detail
        assert not marker.exists(), (
            "C2 OPEN: the project's pytest.ini addopts plugin EXECUTED"
        )

    def test_shipped_setup_cfg_addopts_plugin_does_not_execute(self, tmp_path):
        """C2's second half (probe M2): `setup.cfg` [tool:pytest] addopts.

        `-c <app-owned ini>` suppresses setup.cfg for the same reason it
        suppresses pytest.ini — the ini files are the same lookup.
        """
        proj, marker = self._project(tmp_path, plugin=True,
                                     marker_name="MARKER_CFG")
        (tmp_path / "proj" / "setup.cfg").write_text(
            "[tool:pytest]\naddopts = -p evilplugin\n")
        # No project pytest.ini at all: setup.cfg is the only config, so the
        # framework detector's setup.cfg branch is what selects pytest.
        config = _make_config()
        result = _check_tests("feature.py", proj, config, syntax_passed=True)
        assert result is not None
        assert result.passed is True, result.detail
        assert not marker.exists(), (
            "C2 OPEN: setup.cfg [tool:pytest] addopts plugin EXECUTED"
        )

    def test_shipped_root_conftest_does_not_execute(self, tmp_path):
        """C2's conftest half (probe M3b): a shipped root conftest.py.

        pytest IMPORTS every conftest on the collection path. `--noconftest`
        for a non-app project is what closes this; the app's own tree keeps
        its conftest (it is our hermeticity guard, and an agent editing it
        gets an approval card).
        """
        proj, marker = self._project(
            tmp_path,
            conftest="from pathlib import Path\n"
                     f"Path({str(tmp_path / 'MARKER_CONFTEST')!r})"
                     ".write_text('conftest ran')\n",
            plugin=False,
        )
        config = _make_config()
        result = _check_tests("feature.py", proj, config, syntax_passed=True)
        assert result is not None
        assert result.passed is True, result.detail
        assert not (tmp_path / "MARKER_CONFTEST").exists(), (
            "C2 OPEN: the project's root conftest.py EXECUTED"
        )

    def test_shipped_tests_conftest_does_not_execute(self, tmp_path):
        """The conftest NEXT TO the test (probe M3b's placement)."""
        proj, _marker = self._project(
            tmp_path,
            test_conftest="from pathlib import Path\n"
                          f"Path({str(tmp_path / 'MARKER_TCONF')!r})"
                          ".write_text('conftest ran')\n",
            plugin=False,
        )
        config = _make_config()
        result = _check_tests("feature.py", proj, config, syntax_passed=True)
        assert result is not None
        assert result.passed is True, result.detail
        assert not (tmp_path / "MARKER_TCONF").exists(), (
            "C2 OPEN: tests/conftest.py EXECUTED"
        )

    def test_the_tier_still_reports_a_real_failure(self, tmp_path):
        """Isolation must not degrade the tier into a rubber stamp.

        A failing test still FAILS with the project's ini in place — the
        tier's verdict comes from the tests, not from the isolation.
        """
        proj, _marker = self._project(
            tmp_path, ini="[pytest]\naddopts = -p evilplugin\n")
        (tmp_path / "proj" / "tests" / "test_feature.py").write_text(
            "def test_broken():\n    assert False\n")
        _TEST_CONFIG_CACHE.clear()
        config = _make_config()
        result = _check_tests("feature.py", proj, config, syntax_passed=True)
        assert result is not None
        assert result.passed is False, (
            f"a failing test PASSED the isolated tier: {result.detail}"
        )

    def test_full_suite_run_still_collects_the_project_tests(self, tmp_path):
        """The isolation must not move collection into the app-owned tier dir.

        With `-c <app ini>` pytest's rootdir becomes the ini's directory. If
        the run degraded to "No tests collected" (exit 5) the tier would
        report ⏭ passed — a false PASS with extra steps. The app-owned ini
        sets no `testpaths`, so pytest collects from the INVOCATION dir
        (cwd = project_path): this asserts a real collection + result.
        """
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / ".crabcakes").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "feature.py").write_text("x = 1\n")
        (proj / "tests" / "test_feature.py").write_text(
            "def test_ok():\n    assert True\n")
        _create_real_venv(str(proj))
        # run_full_suite with NO custom command → the auto-detect branch.
        _write_enforcement_json(str(proj), {
            "test": {"run_full_suite": True, "venv_path": ".venv"},
        })
        config = _make_config()
        result = _check_tests("feature.py", str(proj), config,
                              syntax_passed=True)
        assert result is not None
        assert "No tests collected" not in result.detail, (
            "the isolated full-suite run collected nothing — the tier is a "
            f"false PASS: {result.detail}"
        )
        assert result.passed is True, result.detail
        # Positive proof of a REAL collection, not just a non-5 exit code.
        assert "1 passed" in (result.output + result.detail), (
            f"the isolated run did not collect the project test: {result.detail}"
        )

    def test_app_owned_ini_is_written_under_the_config_dir(self, tmp_path,
                                                          monkeypatch):
        """The ini lives at get_config_dir()/tier/pytest.ini — outside any
        project sandbox, so a checked project cannot rewrite it."""
        from agent.enforcement import _TIER_PYTEST_INI, _tier_pytest_config

        monkeypatch.setattr("utils.config.get_config_dir",
                            lambda: str(tmp_path / "cfg"))
        cfg = _tier_pytest_config()
        assert cfg is not None
        ini, tier_dir = cfg
        assert ini == os.path.join(str(tmp_path / "cfg"), "tier", "pytest.ini")
        assert os.path.isfile(ini)
        with open(ini, encoding="utf-8") as f:
            assert f.read() == _TIER_PYTEST_INI
        # The app-owned ini declares NO addopts — that is the whole point.
        lines = [ln.strip() for ln in _TIER_PYTEST_INI.splitlines()
                 if not ln.strip().startswith("#")]
        assert "addopts =" in lines
        assert not any(ln.startswith("addopts =") and ln != "addopts ="
                       for ln in lines), (
            "the app-owned tier ini must declare an EMPTY addopts"
        )

    def test_isolation_args_for_a_foreign_project(self, tmp_path, monkeypatch):
        """The argv shape: -c <app ini>, --confcutdir <app dir>, --noconftest."""
        from agent.enforcement import _pytest_isolation_args

        monkeypatch.setattr("utils.config.get_config_dir",
                            lambda: str(tmp_path / "cfg"))
        args = _pytest_isolation_args("/some/foreign/project")
        assert "-c" in args
        ini = args[args.index("-c") + 1]
        assert ini.endswith(os.path.join("tier", "pytest.ini"))
        assert "--confcutdir" in args
        assert "--noconftest" in args

    def test_isolation_args_keep_the_app_conftest(self, tmp_path, monkeypatch):
        """The APP's own tree keeps its conftest — it is our guard, and Fix 2
        gates edits to it."""
        from agent.enforcement import _APP_ROOT, _pytest_isolation_args

        monkeypatch.setattr("utils.config.get_config_dir",
                            lambda: str(tmp_path / "cfg"))
        args = _pytest_isolation_args(_APP_ROOT)
        assert "-c" in args
        assert "--noconftest" not in args, (
            "the app's own conftest is its hermeticity guard — do not drop it"
        )

    def test_injection_covers_the_pytest_and_python_m_pytest_shapes(self,
                                                                    tmp_path,
                                                                    monkeypatch):
        """Both spellings a project can use get the isolation, positioned so
        nothing later on the command line can override it."""
        from agent.enforcement import _inject_pytest_isolation

        monkeypatch.setattr("utils.config.get_config_dir",
                            lambda: str(tmp_path / "cfg"))
        argv, env = _inject_pytest_isolation(
            ["python3", "-m", "pytest", "tests/test_x.py", "-v"],
            "/some/foreign/project")
        assert argv[:3] == ["python3", "-m", "pytest"]
        assert argv[3] == "-c"
        assert "tests/test_x.py" in argv
        assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"

        bare, _env = _inject_pytest_isolation(
            ["/usr/bin/pytest", "tests/test_x.py"], "/some/foreign/project")
        assert bare[0] == "/usr/bin/pytest"
        assert bare[1] == "-c"

    def test_non_pytest_argv_is_untouched_but_env_still_set(self, tmp_path):
        """A project `command` pointing at another allowlisted binary keeps
        its argv (we do not inject pytest flags into ruff), while the
        plugin-autoload suppression still rides along — it is inert there."""
        from agent.enforcement import _inject_pytest_isolation

        argv, env = _inject_pytest_isolation(["ruff", "check", "x.py"],
                                            "/some/foreign/project")
        assert argv == ["ruff", "check", "x.py"]
        assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in env

    def test_extra_env_off_allowlist_is_dropped(self, tmp_path, monkeypatch):
        """`_run_timed_command` forwards only allowlisted extra vars.

        The widening guard: a future caller cannot smuggle a code-executing
        variable (PYTEST_ADDOPTS, PYTHONPATH, NODE_OPTIONS) through
        *extra_env*.
        """
        from agent.enforcement import _run_timed_command

        monkeypatch.setattr(
            "agent.enforcement._get_scrubbed_env",
            lambda: {"PATH": os.environ.get("PATH", "")},
        )
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            import subprocess as _sp
            return _sp.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr("agent.enforcement.subprocess.run", fake_run)
        # argv[0] must pass the resolved-binary gate; python3 is a system bin.
        run = _run_timed_command(["python3", "-c", "pass"], str(tmp_path), 10,
                                 extra_env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                                            "PYTEST_ADDOPTS": "-p evil"})
        assert run is not None, "gate refused python3 unexpectedly"
        env = captured["env"]
        assert env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
        assert "PYTEST_ADDOPTS" not in env, (
            "a non-allowlisted extra var reached the subprocess env"
        )


# ── CONSOLIDATED audit Fix 7a (M5): the lint tier emits no npx ──────────────


class TestConsolidatedFix7aLintTier:
    """M5: `_detect_linter` never emits `npx`; `_check_lint` validates argv."""

    def test_detect_linter_never_emits_npx(self, tmp_path, monkeypatch):
        """`npx` is a package RUNNER — a registry fetch-and-execute.

        SPEC-66 §11.2 removed it from _ALLOWED_BINARIES with "Do not re-add
        it"; the JS lint path was missed. It now emits the resolved eslint
        binary, or nothing at all when eslint is absent.
        """
        from agent.enforcement import _detect_linter

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "eslint.config.js").write_text("export default [];\n")

        # eslint present → the RESOLVED path, never the npx shim.
        fake_eslint = str(tmp_path / "bin" / "eslint")
        monkeypatch.setattr("agent.enforcement.shutil.which",
                            lambda name: fake_eslint if name == "eslint" else None)
        detected = _detect_linter("src/a.js", str(proj))
        assert detected is not None
        _name, argv = detected
        assert argv[0] == fake_eslint, argv
        assert "npx" not in " ".join(argv)

        # eslint absent → NO JS lint tier. Dropping it is the trade.
        monkeypatch.setattr("agent.enforcement.shutil.which", lambda name: None)
        assert _detect_linter("src/a.js", str(proj)) is None

    def test_lint_pass_through_validate_test_command(self, tmp_path, monkeypatch):
        """The audit's I6a: `_check_lint` never called the token allowlist.

        An npx-first argv (however it got there) must be a visible REFUSED
        check, not an execution.
        """
        from agent.enforcement import _check_lint

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "src").mkdir()
        (proj / "src" / "a.js").write_text("let x = 1;\n")

        monkeypatch.setattr(
            "agent.enforcement._detect_linter",
            lambda fp, pp: ("eslint", ["npx", "eslint", "src/a.js"]),
        )
        monkeypatch.setattr("agent.enforcement.shutil.which",
                            lambda name: "/usr/bin/" + name)

        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("the lint tier EXECUTED an npx argv")

        monkeypatch.setattr("agent.enforcement._run_timed_command", explode)

        result = _check_lint("src/a.js", str(proj), _make_config(), True)
        assert result is not None
        assert result.passed is False
        assert "REFUSED" in result.detail
        assert "allowlisted" in result.detail

    def test_lint_allowlisted_argv_still_runs(self, tmp_path, monkeypatch):
        """Control: a legitimate eslint argv is NOT blocked by the validator."""
        from agent.enforcement import _check_lint

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "src").mkdir()
        (proj / "src" / "a.js").write_text("let x = 1;\n")

        monkeypatch.setattr(
            "agent.enforcement._detect_linter",
            lambda fp, pp: ("eslint", ["/usr/bin/eslint", "src/a.js"]),
        )
        monkeypatch.setattr("agent.enforcement.shutil.which",
                            lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(
            "agent.enforcement._validate_resolved_binary",
            lambda argv, project: (True, argv[0]),
        )
        import subprocess as _sp
        monkeypatch.setattr(
            "agent.enforcement._run_timed_command",
            lambda argv, project, timeout, extra_env=None: (
                _sp.CompletedProcess(argv, 0, b"", b""), 5),
        )
        result = _check_lint("src/a.js", str(proj), _make_config(), True)
        assert result is not None
        assert result.passed is True, result.detail

    def test_test_framework_detector_never_emits_npx(self, tmp_path, monkeypatch):
        """The SAME defect in the TESTS tier: `npx jest` / `npx vitest`.

        A cloned repository's package.json selected this branch, and the argv
        then skipped `_validate_test_command` entirely (the auto-detect path
        never calls it). `npx` is a registry fetch-and-execute, so it is the
        same M5 defect one tier over.
        """
        from agent.enforcement import _detect_test_framework

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "package.json").write_text(
            '{"devDependencies": {"jest": "^29.0.0"}}')

        fake_jest = str(tmp_path / "bin" / "jest")
        monkeypatch.setattr("agent.enforcement.shutil.which",
                            lambda name: fake_jest if name == "jest" else None)
        detected = _detect_test_framework(str(proj))
        assert detected is not None
        name, argv = detected
        assert name == "jest"
        assert argv[0] == fake_jest, argv
        assert "npx" not in " ".join(argv)

        # No jest on PATH → no JS test tier at all.
        monkeypatch.setattr("agent.enforcement.shutil.which", lambda name: None)
        assert _detect_test_framework(str(proj)) is None

        # vitest is the same branch.
        (proj / "package.json").write_text(
            '{"devDependencies": {"vitest": "^1.0.0"}}')
        fake_vitest = str(tmp_path / "bin" / "vitest")
        monkeypatch.setattr("agent.enforcement.shutil.which",
                            lambda name: fake_vitest if name == "vitest" else None)
        detected = _detect_test_framework(str(proj))
        assert detected is not None
        assert detected[0] == "vitest"
        assert "npx" not in " ".join(detected[1])


# ── CONSOLIDATED audit Fix 8 (L-C) + C-B: the pytest flag grammar ────────────
#
# L-C: with Fix 8 reverted (`_validate_test_command` back to the HEAD
# version, which had no per-binary rules at all) the whole of
# tests/test_enforcement.py still passed. These tests fail under that revert
# — and under the narrower C-B defect the audit found, where the rules keyed
# on `base == "pytest"` while the tier's own argv is `python3 -m pytest`.


class TestConsolidatedFix8PytestFlagGrammar:
    """Fix 8 (M6): per-binary flags, keyed on the MODULE (C-B)."""

    @pytest.mark.parametrize("command", [
        "pytest -p evilplugin",                 # the bare spelling Fix 8 closed
        "pytest --plugin evilplugin",
        "pytest -pno:cacheprovider",            # value-attached short form
        "pytest -c evil.ini",
        "pytest --config evil.ini",
        # C-B: the tier's OWN argv shape. Pre-fix these were all True, so the
        # rules that refused the bare spelling never fired on the real argv.
        "python3 -m pytest -p evilplugin",
        "python3 -m pytest --plugin evilplugin",
        "python3 -m pytest -c evil.ini",
        "python3 -m pytest --config evil.ini",
    ])
    def test_pytest_plugin_and_config_flags_are_refused(self, command):
        from agent.enforcement import _validate_test_command

        assert _validate_test_command(command) is False, command

    @pytest.mark.parametrize("command", [
        "pytest tests/",
        "pytest -q -x tests/",
        "python3 -m pytest tests/",
        "python3 -m pytest -q",
        "python3 -m pytest --tb=short tests/",
    ])
    def test_ordinary_pytest_commands_stay_allowed(self, command):
        """The boundary: over-refusal must not swallow ordinary pytest flags."""
        from agent.enforcement import _validate_test_command

        assert _validate_test_command(command) is True, command

    def test_module_keyed_rules_see_the_tier_default_shape(self):
        """C-B: the refusal is the SAME rule, not a second spelling table.

        `python3 -m pytest -p evil` and `pytest -p evil` must produce the
        same refusal — the module resolution is what makes that true.
        """
        from agent.enforcement import _module_base, argv_refusal

        assert _module_base("python3", ["-m", "pytest", "-p", "x"]) == ("pytest", ["-p", "x"])
        assert argv_refusal(["python3", "-m", "pytest", "-p", "evil"]) is not None
        assert argv_refusal(["pytest", "-p", "evil"]) is not None
        # An UNKNOWN module is not a pytest invocation — it stays a python
        # invocation, so the rules do not silently apply to something else.
        assert _module_base("python3", ["-m", "mymod", "-p", "x"]) == (
            "python3", ["-m", "mymod", "-p", "x"])


class TestConsolidatedCBExtraArgsValidated:
    """C-B: `test.extra_args` reaches the tier argv — so it is validated.

    Pre-fix: `extra_args: "-p evilplugin"` imported and executed project code
    while the tier reported PASSED (probe 4, P1). A refusal must be a
    VISIBLE FAILED tier naming the surface — never a silent skip (a skip
    false-PASSes the write) and never a pass.
    """

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _project_with_extra_args(self, tmp_path, extra_args: str):
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / ".crabcakes").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "feature.py").write_text("x = 1\n")
        (proj / "tests" / "test_feature.py").write_text(
            "def test_ok():\n    assert True\n")
        marker = tmp_path / "MARKER_PLUGIN"
        # A root-level plugin module is importable: `python -m` puts the cwd
        # on sys.path[0] (the audit's syspath probe).
        (proj / "evilplugin.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('plugin ran')\n")
        _create_real_venv(str(proj))
        _write_enforcement_json(str(proj), {
            "test": {"extra_args": extra_args, "venv_path": ".venv"},
        })
        return str(proj), marker

    def test_extra_args_plugin_is_a_visible_failed_tier(self, tmp_path):
        proj, marker = self._project_with_extra_args(tmp_path, "-p evilplugin")
        result = _check_tests("feature.py", proj, _make_config(), syntax_passed=True)
        assert result is not None, "extra_args refusal SKIPPED the tier (false PASS)"
        assert result.passed is False, result.detail
        assert "REFUSED" in result.detail
        assert "extra_args" in result.detail, result.detail
        assert not marker.exists(), "C-B OPEN: project-chosen plugin EXECUTED"

    def test_extra_args_plugin_with_the_tier_default_shape(self, tmp_path):
        """C-B item 2's shape: the tier argv is `python3 -m pytest`.

        This is the row that stays open if the rules key on `base ==
        "pytest"` instead of the module: the extra_args token is appended
        after `-m pytest`, so a base-keyed rule never sees it.
        """
        proj, marker = self._project_with_extra_args(tmp_path, "--plugin evilplugin")
        result = _check_tests("feature.py", proj, _make_config(), syntax_passed=True)
        assert result is not None
        assert result.passed is False, result.detail
        assert "REFUSED" in result.detail
        assert not marker.exists(), "C-B OPEN: --plugin EXECUTED"

    def test_ordinary_extra_args_still_run(self, tmp_path):
        """Control: the gate does not degrade the tier into a refusal machine."""
        proj, _marker = self._project_with_extra_args(tmp_path, "-x -q")
        result = _check_tests("feature.py", proj, _make_config(), syntax_passed=True)
        assert result is not None
        assert result.passed is True, result.detail

    def test_extra_args_validator_refuses_unparseable(self):
        """An unparseable fragment is not trusted (same posture as `command`)."""
        from agent.enforcement import _validate_test_extra_args

        assert _validate_test_extra_args("-x -q", ["python3", "-m", "pytest"]) is None
        assert _validate_test_extra_args("", ["python3", "-m", "pytest"]) is None
        assert _validate_test_extra_args(
            "-p 'evil", ["python3", "-m", "pytest"]) is not None


class TestCCPythonPositionalThroughTheRealTier:
    """C-C (PM RULING 2026-11-23) through `_check_tests`, end to end.

    The unit tests pin `_validate_test_command`; this class pins the TIER:
    a project whose `enforcement.json` `test.command` is `python3 runner.py`
    must produce a visible FAILED tests check, and `runner.py` must not run.
    A marker file written by the runner is the proof — if the command ever
    executes, the marker exists.
    """

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _project(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / ".crabcakes").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "feature.py").write_text("x = 1\n")
        (proj / "tests" / "test_feature.py").write_text(
            "def test_ok():\n    assert True\n")
        return proj

    def test_python_script_command_is_a_visible_failed_tier(self, tmp_path):
        proj = self._project(tmp_path)
        marker = tmp_path / "MARKER_C_C"
        # The script writes the marker the instant it runs — so its absence
        # proves the refusal, not merely a failing script.
        (proj / "runner.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n")
        _write_enforcement_json(str(proj), {
            "test": {"command": "python3 runner.py", "venv_path": ".venv"},
        })

        result = _check_tests("feature.py", str(proj), _make_config(),
                              syntax_passed=True)
        assert result is not None, (
            "C-C: a refused test.command SKIPPED the tier instead of failing it")
        assert result.passed is False, result.detail
        assert result.tier == "tests"
        assert "REFUSED" in result.detail, result.detail
        assert "positional" in result.detail, result.detail
        assert not marker.exists(), "C-C OPEN: python3 runner.py EXECUTED"

    def test_module_command_still_runs(self, tmp_path):
        """Control: the ban does not disable the ordinary `-m pytest` shape."""
        proj = self._project(tmp_path)
        _create_real_venv(str(proj))
        _write_enforcement_json(str(proj), {
            "test": {"command": "python3 -m pytest {test_file} -q",
                     "venv_path": ".venv"},
        })

        result = _check_tests("feature.py", str(proj), _make_config(),
                              syntax_passed=True)
        assert result is not None
        assert result.passed is True, result.detail


class TestConsolidatedCB1PytestPositionalRefused:
    """C-B-1 (PM RULING 2026-11-23): a pytest positional is an IMPORT target.

    C-B closed the FLAG half of this surface (`-p`, `--plugin`, `-c`); the
    positional half was the spelling still admitted, and pytest imports the
    file it is told to collect — `pytest evil.py` runs `evil.py` while the
    tier reports PASSED.

    The rule applies to PROJECT-SUPPLIED argv only. The tier appends its own
    related-test path as a positional, so a rule over the ASSEMBLED argv would
    refuse the tier's own default invocation and silently disable the tests
    tier everywhere (the TRAP). `_check_tests` validates before it appends;
    the controls here are the proof that an ordinary project still RUNS.
    """

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _project(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / ".crabcakes").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n")
        (proj / "feature.py").write_text("x = 1\n")
        (proj / "tests" / "test_feature.py").write_text(
            "def test_ok():\n    assert True\n")
        return proj

    def test_unit_pytest_positional_is_refused(self):
        """Both spellings, because the rules key on the MODULE (C-B)."""
        from agent.enforcement import argv_refusal

        for argv in (["pytest", "evil.py"],
                     ["python3", "-m", "pytest", "evil.py"]):
            reason = argv_refusal(argv)
            assert reason is not None, f"C-B-1 OPEN: {argv} admitted"
            assert "import target" in reason, reason

    def test_unit_pytest_flags_only_are_allowed(self):
        """Control: the rule is about the positional, not about pytest."""
        from agent.enforcement import argv_refusal

        for argv in (["pytest", "-q"], ["python3", "-m", "pytest", "-q"],
                     ["python3", "-m", "pytest"], ["pytest", "tests/"],
                     ["pytest", "tests"]):
            assert argv_refusal(argv) is None, argv

    def test_extra_args_pytest_positional_is_a_visible_failed_tier(self, tmp_path):
        """The payload is a ROOT-LEVEL module pytest would import."""
        proj = self._project(tmp_path)
        marker = tmp_path / "MARKER_CB1"
        (proj / "evil.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n")
        _create_real_venv(str(proj))
        _write_enforcement_json(str(proj), {
            "test": {"extra_args": "evil.py", "venv_path": ".venv"},
        })

        result = _check_tests("feature.py", str(proj), _make_config(),
                              syntax_passed=True)
        assert result is not None, (
            "C-B-1: a refused extra_args SKIPPED the tier (false PASS)")
        assert result.passed is False, result.detail
        assert "REFUSED" in result.detail, result.detail
        assert "extra_args" in result.detail, result.detail
        assert not marker.exists(), "C-B-1 OPEN: pytest imported the payload"

    def test_full_suite_command_pytest_positional_is_a_visible_failed_tier(
        self, tmp_path
    ):
        """Third field: the full-suite template is project-supplied too."""
        proj = self._project(tmp_path)
        marker = tmp_path / "MARKER_CB1_FULL"
        (proj / "evil.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n")
        _create_real_venv(str(proj))
        _write_enforcement_json(str(proj), {
            "test": {"full_suite_command": "python3 -m pytest evil.py",
                     "run_full_suite": True, "venv_path": ".venv"},
        })

        result = _check_tests("feature.py", str(proj), _make_config(),
                              syntax_passed=True)
        assert result is not None, "C-B-1: refusal skipped the tier"
        assert result.passed is False, result.detail
        assert "REFUSED" in result.detail, result.detail
        assert not marker.exists(), "C-B-1 OPEN: full_suite imported the payload"

    def test_ordinary_project_tests_tier_still_runs(self, tmp_path):
        """THE CONTROL (brief §1): no enforcement test section at all.

        This is the run the TRAP would break: the tier builds its own argv,
        appends the related test path, and must still pass.
        """
        proj = self._project(tmp_path)
        _create_real_venv(str(proj))

        result = _check_tests("feature.py", str(proj), _make_config(),
                              syntax_passed=True)
        assert result is not None, "the tests tier did not run at all"
        assert result.passed is True, result.detail
        assert "test_feature.py" in result.detail, result.detail

    def test_ordinary_template_project_tests_tier_still_runs(self, tmp_path):
        """THE CONTROL for `{test_file}`: the shipped template shape.

        `python3 -m pytest {test_file} -q` must NOT be refused — the token the
        project wrote is `{test_file}`, and the tier substitutes it after
        validation.
        """
        proj = self._project(tmp_path)
        _create_real_venv(str(proj))
        _write_enforcement_json(str(proj), {
            "test": {"command": "python3 -m pytest {test_file} -q",
                     "venv_path": ".venv"},
        })

        result = _check_tests("feature.py", str(proj), _make_config(),
                              syntax_passed=True)
        assert result is not None, "the tests tier did not run at all"
        assert result.passed is True, result.detail


class TestConsolidatedM1TestsDirConvention:
    """M-1 (re-audit): `<src_dir>/__tests__/{module}.py` is a gate bypass.

    That is `_find_related_test`'s FOURTH candidate, and its basename is just
    the module name — neither `test_*.py` nor `*_test.py`. The gate matched
    names, the tier follows conventions. Closed by ONE shared definition
    (`agent.enforcement.is_tier_test_target`) that both readers use.
    """

    def test_unit_tests_dir_file_is_a_test_target(self):
        from agent.enforcement import is_tier_test_target

        assert is_tier_test_target("src/__tests__/feature.py") is True
        assert is_tier_test_target("__tests__/feature.py") is True
        # Lookalikes that are NOT under a `__tests__` directory stay out.
        for name in ("src/feature.py", "src/tests/feature.py",
                     "src/__test__/feature.py", "src/__tests__/feature.js",
                     "src/__tests__/feature.txt"):
            assert is_tier_test_target(name) is False, name

    def test_unit_tier_and_gate_agree_on_every_candidate(self, tmp_path):
        """THE point of M-1: one definition, so the two cannot drift.

        Every path `_find_related_test` actually RETURNS for a project must be
        a path the write gate treats as a test file. Each candidate is forced
        in turn by removing the ones that would match first.
        """
        from agent.enforcement import _find_related_test
        from agent.tools import is_test_file_name

        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / "src").mkdir()
        (proj / "src" / "feature.py").write_text("x = 1\n")

        # (where the test lives, the files that must NOT exist for this row).
        rows = [
            ("tests/test_feature.py", []),
            ("tests/feature_test.py", ["tests/test_feature.py"]),
            ("src/test_feature.py",
             ["tests/test_feature.py", "tests/feature_test.py"]),
            ("src/__tests__/feature.py",
             ["tests/test_feature.py", "tests/feature_test.py",
              "src/test_feature.py"]),
        ]
        for rel, shadowed in rows:
            for stale in shadowed:
                (proj / stale).unlink(missing_ok=True)
            path = proj / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_ok():\n    assert True\n")
            found = _find_related_test("src/feature.py", str(proj))
            assert found == rel.replace("/", os.sep), (rel, found)
            assert is_test_file_name(found) is True, found


class TestConsolidatedMEFullSuiteCommandIsLive:
    """M-E: `full_suite_command` alone was DEAD (only read inside the
    `if test_config.command:` branch). RESOLUTION: made live on its own.

    The project below has NO detectable framework (no ini, no pyproject, no
    setup.cfg, no package.json, no Makefile), so `_detect_test_framework`
    returns None. Pre-fix the tier therefore returned None — no check at
    all; post-fix the configured full suite runs and reports.
    """

    def setup_method(self):
        _TEST_CONFIG_CACHE.clear()
        _ENFORCEMENT_CONFIG_CACHE.clear()

    def _project(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / ".crabcakes").mkdir()
        (proj / "feature.py").write_text("x = 1\n")
        # `other.py` deliberately has NO related test, so the full-suite
        # branch is the one that produces the check (otherwise the detail
        # would name the related test and prove nothing about the branch).
        (proj / "other.py").write_text("y = 2\n")
        (proj / "tests" / "test_feature.py").write_text(
            "def test_ok():\n    assert True\n")
        _create_real_venv(str(proj))
        return str(proj)

    def test_full_suite_command_runs_with_no_command_field(self, tmp_path):
        from agent.enforcement import _detect_test_framework

        proj = self._project(tmp_path)
        assert _detect_test_framework(proj) is None, (
            "the discriminator needs an undetectable framework")
        _write_enforcement_json(proj, {
            "test": {
                "run_full_suite": True,
                "full_suite_command": "python3 -m pytest tests/ -q",
                "venv_path": ".venv",
            },
        })
        result = _check_tests("other.py", proj, _make_config(), syntax_passed=True)
        assert result is not None, (
            "M-E OPEN: full_suite_command alone produced NO check"
        )
        assert result.passed is True, result.detail
        assert "Full test suite" in result.detail, result.detail

    def test_unsafe_full_suite_command_is_still_refused(self, tmp_path):
        """The now-live field keeps its token-allowlist gate.

        P3: this used to accept `result is None or result.passed is False`,
        i.e. it tolerated the silent skip. A skip reports no tests check at
        all, which reads as a clean write — so the assertion is tightened to
        the visible FAILED check the refusal must now produce.
        """
        marker = tmp_path / "PWNED_ME"
        proj = self._project(tmp_path)
        _write_enforcement_json(proj, {
            "test": {
                "run_full_suite": True,
                "full_suite_command": f"sh -c 'touch {marker}'",
                "venv_path": ".venv",
            },
        })
        result = _check_tests("other.py", proj, _make_config(), syntax_passed=True)
        assert result is not None, "refused full_suite_command skipped silently"
        assert result.passed is False
        assert result.detail.startswith("REFUSED: test.full_suite_command"), result.detail
        assert not marker.exists()
        assert not os.path.exists("/tmp/PWNED_ME")

    def test_command_field_still_wins_when_both_are_set(self, tmp_path):
        """Unchanged behavior for projects that already set `command`."""
        proj = self._project(tmp_path)
        _write_enforcement_json(proj, {
            "test": {
                "command": "python3 -m pytest {test_file} -q",
                "full_suite_command": "python3 -m pytest tests/ -q",
                "run_full_suite": True,
                "venv_path": ".venv",
            },
        })
        result = _check_tests("other.py", proj, _make_config(), syntax_passed=True)
        assert result is not None
        assert result.passed is True, result.detail
        # run_full_suite + full_suite_command is the documented precedence.
        assert "Full test suite" in result.detail, result.detail

    # ── P3: a refused full_suite_command is a visible FAILED tier ──

    def test_p3_refused_full_suite_alone_is_visible_failed(self, tmp_path):
        """Only `full_suite_command` set (no `command`), refused value →
        exactly one tests check, FAILED, naming the field. Pre-fix this
        branch returned None and the write read as clean."""
        marker = tmp_path / "P3_ALONE_RAN"
        proj = self._project(tmp_path)
        _write_enforcement_json(proj, {
            "test": {
                "run_full_suite": True,
                "full_suite_command": f"sh -c 'touch {marker}'",
                "venv_path": ".venv",
            },
        })
        result = check(
            "write_file", {"path": "other.py"},
            ToolResult(success=True, output="", error="", duration_ms=1,
                       stdout="", stderr="", exit_code=0),
            proj, _make_config(),
        )
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1, [c.tier for c in result.checks]
        assert tests_checks[0].passed is False, tests_checks[0].detail
        assert tests_checks[0].detail.startswith(
            "REFUSED: test.full_suite_command"), tests_checks[0].detail
        assert "sh" in tests_checks[0].detail, tests_checks[0].detail
        assert not marker.exists(), "the refused command was EXECUTED"

    def test_p3_refused_full_suite_beats_command_field(self, tmp_path):
        """`command` also set + `run_full_suite` true + refused
        `full_suite_command` → the FIRST full_suite branch refuses visibly
        (that branch shadowed `command`, so its silent skip hid a live
        project field)."""
        marker = tmp_path / "P3_BOTH_RAN"
        proj = self._project(tmp_path)
        _write_enforcement_json(proj, {
            "test": {
                "command": "python3 -m pytest {test_file} -q",
                "run_full_suite": True,
                "full_suite_command": f"sh -c 'touch {marker}'",
                "venv_path": ".venv",
            },
        })
        result = check(
            "write_file", {"path": "other.py"},
            ToolResult(success=True, output="", error="", duration_ms=1,
                       stdout="", stderr="", exit_code=0),
            proj, _make_config(),
        )
        tests_checks = [c for c in result.checks if c.tier == "tests"]
        assert len(tests_checks) == 1, [c.tier for c in result.checks]
        assert tests_checks[0].passed is False, tests_checks[0].detail
        assert tests_checks[0].detail.startswith(
            "REFUSED: test.full_suite_command"), tests_checks[0].detail
        assert not marker.exists(), "the refused command was EXECUTED"

    def test_p3_allowed_full_suite_is_not_refused(self, tmp_path):
        """Control: an allowed `full_suite_command` still runs and passes —
        the refusal must not swallow legitimate commands."""
        proj = self._project(tmp_path)
        _write_enforcement_json(proj, {
            "test": {
                "run_full_suite": True,
                "full_suite_command": "python3 -m pytest tests/ -q",
                "venv_path": ".venv",
            },
        })
        result = _check_tests("other.py", proj, _make_config(), syntax_passed=True)
        assert result is not None
        assert result.passed is True, result.detail
        assert "REFUSED" not in result.detail, result.detail
        assert "Full test suite" in result.detail, result.detail

    def test_p3_refusal_reports_for_both_full_suite_spellings(self, tmp_path):
        """The reachable `full_suite_command` refusal configs must all report
        — reverting the executed branch to `return None` fails here.

        NOTE ON BRANCH 2: the bare `elif test_config.full_suite_command`
        site is currently UNREACHABLE. Any config that could get there is
        already consumed by the guard `if related_test is None and not
        run_full_suite: return None` or by the first `run_full_suite and
        full_suite_command` branch, so both cases below actually exercise
        the first branch. Branch 2 is still converted (it must not be a
        silent skip if it ever becomes reachable); the structural test that
        follows is what pins it.
        """
        marker = tmp_path / "P3_REVERT_RAN"
        refused = f"sh -c 'touch {marker}'"
        cases = {
            "with_command": {
                "command": "python3 -m pytest {test_file} -q",
                "run_full_suite": True,
                "full_suite_command": refused,
                "venv_path": ".venv",
            },
            "without_command": {
                "run_full_suite": True,
                "full_suite_command": refused,
                "venv_path": ".venv",
            },
        }
        for name, test_cfg in cases.items():
            _write_enforcement_json(self._project(tmp_path / name), {"test": test_cfg})
            proj = str(tmp_path / name / "proj")
            result = _check_tests("other.py", proj, _make_config(),
                                  syntax_passed=True)
            assert result is not None, f"{name}: refused full_suite_command skipped silently"
            assert result.passed is False, f"{name}: {result.detail}"
            assert result.detail.startswith(
                "REFUSED: test.full_suite_command"), f"{name}: {result.detail}"
        assert not marker.exists(), "a refused command was EXECUTED"

    def test_p3_all_three_refusal_sites_use_the_shared_helper(self):
        """Revert detector for every refusal site, including the currently
        unreachable branch-2 `elif`: each `if not _validate_test_command(...)`
        body must return through `_tests_refused_check`, and no such body may
        contain a `return None`. Restoring the silent skip at any site fails
        here even though only two sites are reachable behaviorally."""
        import inspect

        src = inspect.getsource(_check_tests)
        assert src.count("_tests_refused_check(") == 3, (
            "a refusal site no longer reports through the shared helper")

        lines = src.splitlines()

        def _refusal_body(idx: int, indent: int) -> str:
            """Lines more-indented than the `if`, i.e. the refusal body."""
            body = []
            for line in lines[idx + 1:]:
                if not line.strip():
                    body.append(line)
                    continue
                if len(line) - len(line.lstrip()) <= indent:
                    break
                body.append(line)
            return "\n".join(body)

        sites = 0
        for i, line in enumerate(lines):
            if "if not _validate_test_command(" not in line:
                continue
            sites += 1
            body = _refusal_body(i, len(line) - len(line.lstrip()))
            assert "_tests_refused_check(" in body, (
                f"refusal at line {i + 1} no longer reports a check:\n{body}")
            assert "return None" not in body, (
                f"refusal at line {i + 1} regressed to a silent skip:\n{body}")
        assert sites == 3, f"expected 3 refusal sites, found {sites}"


