# agent/enforcement.py
# Enforcement Layer — post-write verification for the agent tool loop.
#
# This module provides a single entry point: check(), called after each
# tool execution in the runtime's tool loop. It runs applicable verification
# tiers (syntax, tests, lint) and returns results that are appended to the
# tool result output.
#
# No imports from ui/. No GTK. Pure logic + subprocess calls.

from __future__ import annotations

import dataclasses
import fnmatch
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Syntax checkers — extension → shell command template ────────────────────

SYNTAX_CHECKERS: dict[str, str] = {
    ".py": "python3 -m py_compile {path}",
    ".js": "node --check {path}",
    ".ts": "npx tsc --noEmit {path}",
    ".jsx": "node --check {path}",
    ".tsx": "npx tsc --noEmit {path}",
    ".sh": "bash -n {path}",
    ".bash": "bash -n {path}",
    ".zsh": "zsh -n {path}",
}

# CRIT-1/CRIT-2: Binary allowlist for project-supplied test/lint commands.
# Enforces that .crabcakes/enforcement.json `full_suite_command` first token
# is one of these. See docs/SPEC-SECURITY-REMEDIATION.md §2.1.
_ALLOWED_BINARIES: frozenset[str] = frozenset({
    "python3", "pytest", "ruff", "mypy", "eslint", "npx", "node", "go",
})

# CRIT-2: Scrubbed environment for all enforcement subprocesses.
# Only safe vars survive; provider API keys, gateway tokens, etc. stripped.
_ALLOWED_ENV_VARS: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LANGUAGES", "TZ", "TMPDIR", "PWD",
})

# SP6 Phase 2 fix round: app identity anchor for the tests-tier interpreter
# fallback (Debugger BUG #1, env-bleed). The sys.executable fallback in
# _resolve_tests_python() is safe ONLY when the checked project IS the
# running app — otherwise develcakes' venv deps leak into a foreign
# project's tier (probe: a foreign project importing nh3 false-PASSED).
# Derived like utils/config.py:64 locates the app root (dirname of the
# package dir above this module), never a hardcoded path. realpath'd so the
# identity comparison below (:554) is symmetric — an abspath-built root with
# a symlink component (checkout reached via symlink) would self-deny
# (Phase 2 re-audit BUG #1).
_APP_ROOT: str = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_scrubbed_env() -> dict[str, str]:
    """Return a minimal env dict for enforcement subprocesses.

    Includes only safe vars (PATH, HOME, LANG, etc.). All provider API keys,
    gateway tokens, and other sensitive env vars are stripped. Used by
    _run_timed_command. (Phase 0 / CRIT-2)

    MED-2 (Phase 6): alias for utils.env_security.get_scrubbed_env. Kept here
    to avoid touching all existing call sites; the canonical implementation
    lives in utils/env_security.py so agent.tools._exec_command can share it.
    """
    from utils.env_security import get_scrubbed_env
    return get_scrubbed_env()


# CRIT-1: Shell metacharacters that must NOT appear in a filename basename.
# Defense-in-depth — _check_syntax interpolates the path into a shell command,
# so a basename with `;`, `|`, backticks, or $() enables RCE.
_SHELL_METACHARS: frozenset[str] = frozenset(";|&`$()<>*?[]{}!\\\"'")


def _is_safe_filename(file_path: str) -> bool:
    """Return True if `file_path`'s basename contains no shell metacharacters.

    CRIT-1 defense-in-depth: rejects filenames like `x;touch evil.py` even if
    the path sandbox would allow them. (Phase 0)
    """
    basename = os.path.basename(file_path)
    if not basename:
        return False
    return not any(c in _SHELL_METACHARS for c in basename)


def _validate_test_command(command: str | None) -> bool:
    """Return True if `command`'s first token is in _ALLOWED_BINARIES.

    Strips leading whitespace, splits on whitespace, lowercases the first token,
    strips path components. Used to gate project-supplied .crabcakes/enforcement.json
    commands. (Phase 0 / CRIT-2)
    """
    if not command or not command.strip():
        return False
    first_token = command.strip().split(maxsplit=1)[0].lower()
    first_token = os.path.basename(first_token)
    return first_token in _ALLOWED_BINARIES

# ── Data models ────────────────────────────────────────────────────────────────


@dataclass
class EnforcementCheck:
    """Single verification check result."""
    tier: str               # "syntax" | "tests" | "lint"
    tool: str              # which tool triggered this ("write_file")
    file: str               # relative path of the file checked
    passed: bool           # True = green, False = red
    detail: str            # human-readable summary
    output: str             # raw command output (truncated)
    duration_ms: int       # how long the check took


@dataclass
class EnforcementResult:
    """Aggregated result from all enforcement checks for one tool call."""
    checks: list[EnforcementCheck] = field(default_factory=list)
    appended_message: str = ""    # formatted message to append to tool result


# ── Skip patterns — applied to basename, fnmatch-style ──────────────────────
# The default skip-pattern list lives in EnforcementConfig.skip_patterns
# (agent/config.py). Per-project overrides merge additively in check().

def _is_skipped(file_path: str, skip_patterns: list[str]) -> bool:
    """Return True if the file matches any skip pattern."""
    basename = os.path.basename(file_path)
    for pattern in skip_patterns:
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


@dataclass
class TestConfig:
    """Per-project test configuration, loaded from .crabcakes/enforcement.json.

    Provides configurable test discovery, venv activation, command templates,
    and timeout overrides. All fields have safe defaults so projects without
    a test section in enforcement.json work out of the box.
    """
    command: str | None = None               # Override test command template ({test_file} placeholder)
    full_suite_command: str | None = None    # Override full suite command
    test_dir: str = "tests"                  # Test directory (relative to project root)
    naming_pattern: str = "test_{module}.py"  # Test file naming pattern ({module} placeholder)
    venv_path: str = ".venv"                 # Venv directory (relative to project root)
    run_full_suite: bool = False             # If true, always run full suite instead of related test
    timeout_seconds: int = 60                # Per-project test timeout override
    extra_args: str = "-x -q"                # Extra pytest/jest arguments

    @classmethod
    def from_dict(cls, data: dict) -> TestConfig:
        """Create TestConfig from enforcement.json test section.

        All numeric and boolean fields are coerced to their correct types.
        Bad values are logged and replaced with defaults rather than crashing
        or silently misbehaving (e.g. string "false" → bool False).
        """
        if not isinstance(data, dict):
            return cls()

        def _bool(key: str, default: bool) -> bool:
            val = data.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            return default

        def _int(key: str, default: int) -> int:
            val = data.get(key)
            if isinstance(val, bool):   # bool is subclass of int — check first
                return default
            if isinstance(val, int):
                return val
            if isinstance(val, str):
                try:
                    return int(val)
                except ValueError:
                    logger.debug("[enforcement] TestConfig: %s=%r is not int, using default %d", key, val, default)
                    return default
            return default

        return cls(
            command=data.get("command"),
            full_suite_command=data.get("full_suite_command"),
            test_dir=data.get("test_dir", "tests"),
            naming_pattern=data.get("naming_pattern", "test_{module}.py"),
            venv_path=data.get("venv_path", ".venv"),
            run_full_suite=_bool("run_full_suite", False),
            timeout_seconds=_int("timeout_seconds", 60),
            extra_args=data.get("extra_args", "-x -q"),
        )


# ── Per-project enforcement config ───────────────────────────────────────────

# TTL cache: project_path → (timestamp, data_or_None)
_ENFORCEMENT_CONFIG_CACHE: dict[str, tuple[float, dict | None]] = {}
_ENFORCEMENT_CONFIG_TTL = 30.0  # seconds

# TTL cache for per-project test config: project_path → (timestamp, TestConfig | None)
_TEST_CONFIG_CACHE: dict[str, tuple[float, TestConfig | None]] = {}


def _load_test_config(project_path: str) -> TestConfig | None:
    """Load per-project test configuration from .crabcakes/enforcement.json.

    Separately cached from the enforcement tier toggles so each can evolve
    independently. Shares the same TTL as the enforcement config.

    Returns TestConfig or None if no test section in config.
    """
    now = time.monotonic()
    cached = _TEST_CONFIG_CACHE.get(project_path)
    if cached is not None:
        ts, data = cached
        if now - ts < _ENFORCEMENT_CONFIG_TTL:
            return data

    cfg_path = os.path.join(project_path, ".crabcakes", "enforcement.json")
    if not os.path.isfile(cfg_path):
        _TEST_CONFIG_CACHE[project_path] = (now, None)
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        test_section = data.get("test")
        if test_section and isinstance(test_section, dict):
            tc = TestConfig.from_dict(test_section)
            _TEST_CONFIG_CACHE[project_path] = (now, tc)
            return tc
        _TEST_CONFIG_CACHE[project_path] = (now, None)
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("[enforcement] test config unreadable: %s", e)
        _TEST_CONFIG_CACHE[project_path] = (now, None)
        return None



def _load_project_enforcement_config(project_path: str) -> dict | None:
    """
    §F — Load per-project enforcement override from .crabcakes/enforcement.json.

    Results are cached for 30 seconds to avoid reading the file on every write.
    Priority: .crabcakes/enforcement.json > agent.json enforcement section > defaults.

    Returns parsed dict or None if file doesn't exist / can't be read.
    """
    now = time.monotonic()
    cached = _ENFORCEMENT_CONFIG_CACHE.get(project_path)
    if cached is not None:
        ts, data = cached
        if now - ts < _ENFORCEMENT_CONFIG_TTL:
            return data

    cfg_path = os.path.join(project_path, ".crabcakes", "enforcement.json")
    if not os.path.isfile(cfg_path):
        _ENFORCEMENT_CONFIG_CACHE[project_path] = (now, None)
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _ENFORCEMENT_CONFIG_CACHE[project_path] = (now, data)
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("[enforcement] per-project config unreadable: %s", e)
        _ENFORCEMENT_CONFIG_CACHE[project_path] = (now, None)
        return None


def _detect_venv_prefix(project_path: str, venv_path: str = ".venv") -> str | None:
    """Return absolute path to venv Python interpreter, or None if no venv.

    Replaces the previous shell-sourcing behavior (which was a CRIT-2 RCE vector —
    a poisoned activate script would run on every enforcement check).
    Callers should substitute `python3 -m pytest` → `<result> -m pytest` when
    this returns a non-None value. (Phase 0 / CRIT-2)
    """
    venv_abs = os.path.join(project_path, venv_path)
    python_abs = os.path.join(venv_abs, "bin", "python")
    if os.path.isfile(python_abs):
        return python_abs
    return None


# ── Tier 1: Syntax Guard ──────────────────────────────────────────────────────


def _check_syntax(
    file_path: str,
    project_path: str,
    config: Any,
    verbose: bool = False,
) -> EnforcementCheck | None:
    """
    Run Tier 1 syntax guard on a file.
    Returns None if skipped (unknown extension, binary, not installed).

    When *verbose* is True and the file matches a skip pattern, returns a
    placeholder EnforcementCheck with a ``SKIPPED:`` detail prefix instead
    of None — so callers can surface the skip reason in the output. When
    verbose is False (default), this function returns None on skip exactly
    as it did before, preserving existing behavior.
    """
    ext = os.path.splitext(file_path)[1].lower()
    checker = SYNTAX_CHECKERS.get(ext)
    if checker is None:
        return None

    if _is_skipped(file_path, config.skip_patterns):
        if verbose:
            return EnforcementCheck(
                tier="syntax", tool="write_file", file=file_path,
                passed=True,
                detail=f"SKIPPED: {file_path} matches skip pattern (syntax tier)",
                output="", duration_ms=0,
            )
        return None

    abs_path = os.path.join(project_path, file_path)
    if not os.path.isfile(abs_path):
        return None

    # Check if required binary is available (skip if not)
    binary = checker.split()[0]
    if binary not in ("python3", "bash") and not shutil.which(binary):
        logger.debug("[enforcement] syntax checker not available: %s", binary)
        return None

    # CRIT-1: defense-in-depth filename check
    if not _is_safe_filename(abs_path):
        return EnforcementCheck(
            tier="syntax", tool="write_file", file=file_path,
            passed=False,
            detail=f"Filename contains shell metacharacters: {os.path.basename(abs_path)}",
            output="", duration_ms=0,
        )

    # Build argv list — no shell=True, no string interpolation
    # Split the template and substitute {path} with the absolute path
    argv = [arg.replace("{path}", abs_path) for arg in checker.split()]

    start = time.monotonic()
    try:
        result = subprocess.run(
            argv, shell=False, capture_output=True,
            timeout=config.syntax_timeout_seconds,
            env=_get_scrubbed_env(),
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        passed = result.returncode == 0

        return EnforcementCheck(
            tier="syntax",
            tool="write_file",
            file=file_path,
            passed=passed,
            detail=f"Syntax check {'passed' if passed else 'FAILED'} for {file_path}",
            output=output[: config.max_output_chars],
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired:
        return EnforcementCheck(
            tier="syntax", tool="write_file", file=file_path,
            passed=False,
            detail=f"Syntax check timed out for {file_path}",
            output="", duration_ms=config.syntax_timeout_seconds * 1000,
        )
    except Exception as e:
        logger.debug("[enforcement] syntax check raised %s: %s", type(e).__name__, e)
        return None


# ── Tier 2: Test Runner ────────────────────────────────────────────────────────


def _detect_test_framework(project_path: str) -> tuple[str, list[str]] | None:
    """
    Detect test framework for a project.
    Returns (framework_name, argv_list) or None. (Phase 0 / CRIT-2)
    """
    # pytest — pyproject.toml with [tool.pytest] or pytest in dependencies
    pyproject = os.path.join(project_path, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            import tomllib
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            if "tool" in data and "pytest" in data["tool"]:
                return ("pytest", ["python3", "-m", "pytest"])
            deps = data.get("project", {}).get("dependencies", [])
            if any("pytest" in str(d) for d in deps):
                return ("pytest", ["python3", "-m", "pytest"])
            opt_deps = data.get("project", {}).get("optional-dependencies", {})
            for extra, deps_list in opt_deps.items():
                if any("pytest" in str(d) for d in deps_list):
                    return ("pytest", ["python3", "-m", "pytest"])
        except Exception:
            pass

    # pytest.ini
    if os.path.isfile(os.path.join(project_path, "pytest.ini")):
        return ("pytest", ["python3", "-m", "pytest"])

    # setup.cfg with [tool:pytest]
    setup_cfg = os.path.join(project_path, "setup.cfg")
    if os.path.isfile(setup_cfg):
        try:
            with open(setup_cfg, "r", encoding="utf-8") as f:
                content = f.read()
            if "[tool:pytest]" in content or "[pytest]" in content:
                return ("pytest", ["python3", "-m", "pytest"])
        except Exception:
            pass

    # Jest — package.json with "jest" in devDependencies
    pkg_json = os.path.join(project_path, "package.json")
    if os.path.isfile(pkg_json):
        try:
            import json
            with open(pkg_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            dev_deps = data.get("devDependencies", {}) or data.get("dependencies", {})
            if "jest" in dev_deps:
                return ("jest", ["npx", "jest", "--no-coverage"])
            if "vitest" in dev_deps:
                return ("vitest", ["npx", "vitest", "run"])
        except Exception:
            pass

    # Makefile with test target
    makefile = os.path.join(project_path, "Makefile")
    if os.path.isfile(makefile):
        try:
            with open(makefile, "r", encoding="utf-8") as f:
                content = f.read()
            if "\ntest:" in content or "\ntest :\n" in content:
                return ("make", ["make", "test"])
        except Exception:
            pass

    return None


def _find_related_test(
    file_path: str,
    project_path: str,
    test_dir: str = "tests",
    naming_pattern: str = "test_{module}.py",
) -> str | None:
    """
    Find the test file corresponding to a source file.
    Uses configurable naming pattern. {module} is replaced with the source
    file's basename (without extension).

    Args:
        file_path: Relative path of the source file within the project.
        project_path: Absolute path to the project root.
        test_dir: Directory containing test files (relative to project root).
        naming_pattern: Pattern for test file names. {module} is replaced with
            the source file's basename without extension.
    """
    basename = os.path.splitext(os.path.basename(file_path))[0]
    pattern = naming_pattern.replace("{module}", basename)

    candidates = [
        os.path.join(project_path, test_dir, pattern),
        os.path.join(project_path, test_dir, f"{basename}_test.py"),  # Jest/Vitest convention
    ]

    # Also check if there's a mirror in the same directory
    src_dir = os.path.dirname(os.path.join(project_path, file_path))
    candidates.extend([
        os.path.join(src_dir, pattern),
        os.path.join(src_dir, "__tests__", f"{basename}.py"),
    ])

    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.relpath(candidate, project_path)

    return None


def _parse_command_to_argv(command: str) -> list[str]:
    """Parse a shell-like command string into an argv list.

    Handles basic quoting (single and double) and whitespace splitting.
    Used to convert project-supplied command strings (from enforcement.json)
    into argv lists for subprocess.run(shell=False). (Phase 0 / CRIT-2)
    """
    import shlex
    try:
        return shlex.split(command)
    except ValueError:
        # Fallback: basic whitespace split if shlex fails
        return command.split()


def _substitute_venv_python(argv: list[str], venv_python: str | None) -> list[str]:
    """Replace 'python3' with venv_python in argv if venv_python is set.

    Used after _detect_venv_prefix returns an absolute path. (Phase 0 / CRIT-2)
    """
    if venv_python is None:
        return argv
    result = list(argv)
    for i, token in enumerate(result):
        if token == "python3":
            result[i] = venv_python
            break
    return result


def _resolve_tests_python(project_path: str, venv_python: str | None) -> str | None:
    """Resolve the tests-tier interpreter when the venv probe missed.

    SP6 Phase 2 (cluster A): a project without ``.venv`` (tmp_path test
    projects; PEP 668 hosts) left argv at bare ``python3``, and the system
    interpreter has no pytest — the tier false-FAILED with
    "No module named pytest". Resolution order:

    1. the project venv python (unchanged — a project's venv always wins);
    2. the RUNNING interpreter (``sys.executable``), ONLY when BOTH hold:
       the checked project IS the running app (``_APP_ROOT`` — identity
       gate, fix round for Debugger BUG #1: substituting develcakes' venv
       python for a FOREIGN project runs that project's tests against
       develcakes' dependency set — probe: foreign project importing nh3
       false-PASSED), and pytest is importable there (fail-closed: a
       pytest-less interpreter returns None so the argv keeps the
       historical bare-``python3`` shape and the tier fails exactly as
       before Phase 2).

    The probe is process-local (``find_spec``, no import executed);
    subprocesses still run through _run_timed_command with the scrubbed env
    (CRIT-2 unchanged).
    """
    if venv_python is not None:
        return venv_python
    if os.path.realpath(project_path) != _APP_ROOT:
        return None
    from importlib.util import find_spec

    if find_spec("pytest") is None:
        return None
    exe = sys.executable
    return exe if exe and os.path.isfile(exe) else None


def _check_tests(
    file_path: str,
    project_path: str,
    config: Any,
    syntax_passed: bool,
    verbose: bool = False,
) -> EnforcementCheck | None:
    """
    Run Tier 2 test runner.
    Returns None if skipped.

    Uses per-project TestConfig from .crabcakes/enforcement.json when available,
    falling back to auto-detection defaults otherwise.

    CRIT-2: All subprocess calls use argv lists + shell=False.
    _ALLOWED_BINARIES gate is applied to project-supplied full_suite_command.
    (Phase 0)

    When *verbose* is True, skip paths return a placeholder EnforcementCheck
    with a ``SKIPPED:`` detail prefix instead of None. When verbose is False
    (default), behavior is unchanged from prior versions.
    """
    # Skip if syntax failed — no point running tests on broken code
    if not syntax_passed:
        return None

    # Skip test files themselves
    basename = os.path.basename(file_path)
    if basename.startswith("test_") or basename.endswith("_test.py"):
        if verbose:
            return EnforcementCheck(
                tier="tests", tool="write_file", file=file_path,
                passed=True,
                detail=f"SKIPPED: {file_path} is itself a test file",
                output="", duration_ms=0,
            )
        return None

    # Skip files matching skip patterns (markdown, configs, etc.)
    if _is_skipped(file_path, config.skip_patterns):
        if verbose:
            return EnforcementCheck(
                tier="tests", tool="write_file", file=file_path,
                passed=True,
                detail=f"SKIPPED: {file_path} matches skip pattern (tests tier)",
                output="", duration_ms=0,
            )
        return None

    # Load per-project test configuration
    test_config = _load_test_config(project_path) or TestConfig()

    # Detect venv python path (CRIT-2 fix: no shell-sourcing). SP6 Phase 2:
    # a venv probe miss falls back to the running interpreter (only when it
    # can import pytest) — bare `python3` on a PEP 668 host has no pytest
    # and false-FAILED the tier ("No module named pytest").
    venv_python = _resolve_tests_python(
        project_path, _detect_venv_prefix(project_path, test_config.venv_path)
    )

    # Determine test timeout (project override or config default)
    test_timeout = test_config.timeout_seconds if test_config.timeout_seconds is not None else config.test_timeout_seconds

    # If a custom command template is provided, use it directly
    if test_config.command:
        related_test = _find_related_test(
            file_path, project_path,
            test_config.test_dir, test_config.naming_pattern,
        )
        if related_test is None and not test_config.run_full_suite:
            return None  # No related test and not running full suite

        if test_config.run_full_suite and test_config.full_suite_command:
            # CRIT-2: validate first token is an allowed binary
            if not _validate_test_command(test_config.full_suite_command):
                logger.warning("[enforcement] full_suite_command uses non-allowed binary: %s",
                                test_config.full_suite_command)
                return None
            argv = _parse_command_to_argv(test_config.full_suite_command)
            argv = _substitute_venv_python(argv, venv_python)
        elif related_test:
            abs_test = os.path.join(project_path, related_test)
            cmd_str = test_config.command.replace("{test_file}", abs_test)
            argv = _parse_command_to_argv(cmd_str)
            argv = _substitute_venv_python(argv, venv_python)
        elif test_config.full_suite_command:
            if not _validate_test_command(test_config.full_suite_command):
                logger.warning("[enforcement] full_suite_command uses non-allowed binary: %s",
                                test_config.full_suite_command)
                return None
            argv = _parse_command_to_argv(test_config.full_suite_command)
            argv = _substitute_venv_python(argv, venv_python)
        else:
            logger.debug("[enforcement] No related test and no full_suite_command — skipping")
            return None
    else:
        # Auto-detect test framework (now returns argv list)
        framework = _detect_test_framework(project_path)
        if framework is None:
            return None
        framework_name, argv = framework

        related_test = _find_related_test(
            file_path, project_path,
            test_config.test_dir, test_config.naming_pattern,
        )

        if test_config.run_full_suite:
            argv = list(argv) + test_config.extra_args.split() + ["--tb=short"]
            argv = _substitute_venv_python(argv, venv_python)
        elif related_test:
            abs_test = os.path.join(project_path, related_test)
            argv = list(argv) + [abs_test] + test_config.extra_args.split() + ["--tb=short"]
            argv = _substitute_venv_python(argv, venv_python)
        else:
            # No related test found — skip unless run_full_suite is true
            return None

    try:
        result, duration_ms = _run_timed_command(argv, project_path, test_timeout)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        # pytest returns exit code 5 when no tests collected
        if result.returncode == 5:
            return EnforcementCheck(
                tier="tests",
                tool="write_file",
                file=file_path,
                passed=True,
                detail=f"⏭ No tests collected for {file_path}",
                output="",
                duration_ms=duration_ms,
            )
        passed = result.returncode == 0

        if related_test:
            detail = f"{related_test}: {'passed' if passed else 'FAILED'}"
        else:
            detail = f"Full test suite: {'passed' if passed else 'FAILED'}"

        return EnforcementCheck(
            tier="tests",
            tool="write_file",
            file=file_path,
            passed=passed,
            detail=detail,
            output=output[: config.max_output_chars],
            duration_ms=duration_ms,
        )

    except subprocess.TimeoutExpired:
        return EnforcementCheck(
            tier="tests", tool="write_file", file=file_path,
            passed=False,
            detail=f"Test run timed out ({test_timeout}s) for {file_path}",
            output="", duration_ms=test_timeout * 1000,
        )
    except Exception as e:
        logger.debug("[enforcement] test check raised %s: %s", type(e).__name__, e)
        return None


# ── Tier 3: Lint Check ─────────────────────────────────────────────────────────


def _detect_linter(file_path: str, project_path: str) -> tuple[str, list[str]] | None:
    """
    Detect linter for this file type.
    Returns (linter_name, argv_list) or None. (Phase 0 / CRIT-2)
    """
    ext = os.path.splitext(file_path)[1].lower()

    # ruff — works for Python
    ruff_config = os.path.join(project_path, "ruff.toml")
    ruff_pyproject = os.path.join(project_path, "pyproject.toml")
    if ext == ".py":
        if os.path.isfile(ruff_config) or os.path.isfile(ruff_pyproject):
            # Check if ruff is referenced in pyproject.toml
            if os.path.isfile(ruff_pyproject):
                try:
                    import tomllib
                    with open(ruff_pyproject, "rb") as f:
                        data = tomllib.load(f)
                    if "tool" in data and "ruff" in data["tool"]:
                        return ("ruff", ["ruff", "check", file_path, "--output-format=concise"])
                except Exception:
                    pass
            return ("ruff", ["ruff", "check", file_path, "--output-format=concise"])

    # mypy — Python type checking
    if ext == ".py":
        pyproject = os.path.join(project_path, "pyproject.toml")
        if os.path.isfile(pyproject):
            try:
                import tomllib
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
                if "tool" in data and "mypy" in data["tool"]:
                    return ("mypy", ["mypy", file_path, "--no-error-summary"])
            except Exception:
                pass

    # eslint — JS/TS
    if ext in (".js", ".jsx", ".ts", ".tsx"):
        eslintrc = os.path.join(project_path, ".eslintrc")
        eslint_config = os.path.join(project_path, "eslint.config.js")
        if os.path.isfile(eslintrc) or os.path.isfile(eslint_config):
            if shutil.which("npx"):
                return ("eslint", ["npx", "eslint", file_path])

    return None


def _run_timed_command(argv: list[str], project_path: str, timeout: int) -> tuple[subprocess.CompletedProcess, int]:
    """Run a subprocess with argv list, shell=False, scrubbed env.

    Returns (result, duration_ms). Raises on timeout.
    CRIT-1/CRIT-2: shell=False is enforced. Env is scrubbed to PATH/HOME/LANG only. (Phase 0)
    """
    start = time.monotonic()
    result = subprocess.run(
        argv, shell=False, capture_output=True,
        cwd=project_path, timeout=timeout,
        env=_get_scrubbed_env(),
    )
    return result, int((time.monotonic() - start) * 1000)


def _check_lint(
    file_path: str,
    project_path: str,
    config: Any,
    syntax_passed: bool,
    verbose: bool = False,
) -> EnforcementCheck | None:
    """
    Run Tier 3 lint check.
    Returns None if skipped.

    When *verbose* is True and the file matches a skip pattern, returns a
    placeholder EnforcementCheck with a ``SKIPPED:`` detail prefix instead
    of None. When verbose is False (default), behavior is unchanged.
    """
    if not syntax_passed:
        return None

    if _is_skipped(file_path, config.skip_patterns):
        if verbose:
            return EnforcementCheck(
                tier="lint", tool="write_file", file=file_path,
                passed=True,
                detail=f"SKIPPED: {file_path} matches skip pattern (lint tier)",
                output="", duration_ms=0,
            )
        return None

    linter = _detect_linter(file_path, project_path)
    if linter is None:
        return None

    linter_name, argv = linter

    # Check if the linter binary is available
    binary = linter_name
    if not shutil.which(binary):
        return None

    start = time.monotonic()
    try:
        result, duration_ms = _run_timed_command(argv, project_path, config.lint_timeout_seconds)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        passed = result.returncode == 0

        return EnforcementCheck(
            tier="lint",
            tool="write_file",
            file=file_path,
            passed=passed,
            detail=f"Lint check {'passed' if passed else 'FAILED'} ({linter_name}, {duration_ms / 1000:.1f}s)",
            output=output[: config.max_output_chars],
            duration_ms=duration_ms,
        )

    except subprocess.TimeoutExpired:
        return EnforcementCheck(
            tier="lint", tool="write_file", file=file_path,
            passed=False,
            detail=f"Lint check timed out for {file_path}",
            output="", duration_ms=config.lint_timeout_seconds * 1000,
        )
    except Exception as e:
        logger.debug("[enforcement] lint check raised %s: %s", type(e).__name__, e)
        return None


# ── Formatter ─────────────────────────────────────────────────────────────────


def _format_result(checks: list[EnforcementCheck], max_output: int) -> str:
    """Format enforcement checks into a message to append to tool result."""
    if not checks:
        return ""

    lines = []
    for check in checks:
        if check.passed:
            icon = "✅"
        else:
            icon = "❌"

        if check.output:
            # Truncate output to max_output chars total
            output_preview = check.output.strip()[:max_output]
            if len(check.output) > max_output:
                output_preview += f"\n[... truncated ...]"
            lines.append(f"[enforcement:{check.tier}] {icon} {check.detail}\n{output_preview}")
        else:
            lines.append(f"[enforcement:{check.tier}] {icon} {check.detail}")

    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────


def check(
    tool_name: str,
    tool_args: dict,
    tool_result,       # ToolResult from the original tool execution
    project_path: str,
    config: Any,        # EnforcementConfig
    verbose: bool = False,
) -> EnforcementResult:
    """
    Main entry point. Called after each tool execution in the tool loop.

    Only acts on write_file calls where the file was successfully written.
    Returns empty result for all other tools.

    When *verbose* is True, files that match a skip pattern (or that are
    themselves test files) produce a placeholder ``EnforcementCheck`` with
    a ``SKIPPED:`` detail prefix in ``result.checks`` and a corresponding
    line in ``appended_message``. This lets callers (debug UIs, audit
    logs) see *why* a file was not checked.

    When verbose is False (default), behavior is unchanged: skipped files
    do not appear in the output, exactly as before.

    Returns:
        EnforcementResult with checks and formatted message to append.
    """
    # Only trigger on file-writing tools that succeeded
    if tool_name not in ("write_file", "edit_file"):
        return EnforcementResult()

    # Only run if the write itself succeeded
    if not tool_result.success:
        return EnforcementResult()

    file_path = tool_args.get("path", "")
    if not file_path:
        return EnforcementResult()

    # §F — Per-project override: load BEFORE any tier checks so all tiers
    # see the overridden config (including syntax_check=False if set)
    project_override = _load_project_enforcement_config(project_path)
    if project_override is not None:
        # Merge project-level skip_patterns (additive to global)
        project_skip = project_override.get("skip_patterns")
        if project_skip and isinstance(project_skip, list):
            merged_skip = list(config.skip_patterns) + project_skip
        else:
            merged_skip = config.skip_patterns

        # Per-tier overrides: if project config explicitly sets a tier to False, skip it
        if not project_override.get("syntax_check", True):
            config = dataclasses.replace(config, syntax_check=False)
        if not project_override.get("test_run", True):
            config = dataclasses.replace(config, test_run=False)
        if not project_override.get("lint_check", True):
            config = dataclasses.replace(config, lint_check=False)
        # Use merged skip patterns
        config = dataclasses.replace(config, skip_patterns=merged_skip)

    checks: list[EnforcementCheck] = []

    # Tier 1: Syntax guard (uses overridden config)
    if config.syntax_check:
        syntax_result = _check_syntax(file_path, project_path, config, verbose=verbose)
        if syntax_result is not None:
            checks.append(syntax_result)

    # Determine if syntax passed (for gating Tier 2/Tier 3)
    # A SKIPPED placeholder (verbose mode) is treated as "passed" for gating
    # purposes — it didn't fail, it just didn't run.
    syntax_passed = all(
        c.tier != "syntax" or c.passed or c.detail.startswith("SKIPPED:")
        for c in checks
    )
    # If no syntax check ran, default to True (don't gate)
    no_syntax_check = all(c.tier != "syntax" for c in checks)
    syntax_gate = syntax_passed or no_syntax_check

    # Tier 2: Test runner
    if config.test_run and syntax_gate:
        tests_result = _check_tests(file_path, project_path, config, syntax_passed, verbose=verbose)
        if tests_result is not None:
            checks.append(tests_result)

    # Tier 3: Lint check
    if config.lint_check and syntax_gate:
        lint_result = _check_lint(file_path, project_path, config, syntax_passed, verbose=verbose)
        if lint_result is not None:
            checks.append(lint_result)

    if not checks:
        return EnforcementResult()

    appended_message = _format_result(checks, config.max_output_chars)
    return EnforcementResult(checks=checks, appended_message=appended_message)