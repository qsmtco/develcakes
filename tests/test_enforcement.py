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
    _detect_venv_prefix,
    _find_related_test,
    _load_test_config,
    _check_tests,
    _resolve_tests_python,
    check,
    _TEST_CONFIG_CACHE,
    _ENFORCEMENT_CONFIG_CACHE,
)
from agent.config import EnforcementConfig
from agent.tools import ToolResult


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
