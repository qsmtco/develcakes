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

from agent.enforcement import (
    TestConfig,
    _APP_ROOT,
    _detect_venv_prefix,
    _find_related_test,
    _is_app_worktree,
    _load_test_config,
    _check_tests,
    _resolve_tests_python,
    check,
    _TEST_CONFIG_CACHE,
    _ENFORCEMENT_CONFIG_CACHE,
)
from agent.config import EnforcementConfig
from agent.tools import ToolResult


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
        assert tc.timeout_seconds == 60

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
        assert tc.timeout_seconds == 60
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

    def test_cache_ttl(self, tmp_path):
        """Test config is cached and reused within TTL."""
        crab_dir = tmp_path / ".crabcakes"
        crab_dir.mkdir()
        (crab_dir / "enforcement.json").write_text(json.dumps({
            "test": {"timeout_seconds": 20}
        }))
        _TEST_CONFIG_CACHE.clear()

        tc1 = _load_test_config(str(tmp_path))
        assert tc1.timeout_seconds == 20

        # Update file — should still return cached value
        (crab_dir / "enforcement.json").write_text(json.dumps({
            "test": {"timeout_seconds": 40}
        }))
        tc2 = _load_test_config(str(tmp_path))
        assert tc2.timeout_seconds == 20  # Still cached

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
        # The tier is silently skipped (return None) on token refusal —
        # visible FAILED for token-refusal is a related finding, not in
        # scope for BUG#6 (BUG#8 skip-DoS stays REGISTERED per the spec).
        assert not tests_checks or tests_checks[0].passed is False

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
        self._write_cfg(str(proj), {
            "command": "pytest tests/test_x.py -q",
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
