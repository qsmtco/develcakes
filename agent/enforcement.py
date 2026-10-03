# agent/enforcement.py
# Enforcement Layer — post-write verification for the agent tool loop.
#
# This module provides a single entry point: check(), called after each
# tool execution in the runtime's tool loop. It runs applicable verification
# tiers (syntax, tests, lint) and returns results that are appended to the
# tool result output.
#
# No imports from ui/. No GTK. Pure logic + subprocess calls.
#
# THREAT MODEL (SPEC-09 SP0 fix round, Supervisor ruling 2026-10-02): the
# gate's contract is "project-supplied config cannot falsify validation" —
# NOT host hardening. Trusted roots for resolved binaries are therefore
# exactly: system bins ∪ realpath(HOME) (operator tooling: ~/.local/bin
# etc.) ∪ the app venv bin (admitted ONLY when the checked project IS the
# running app or one of its git worktrees) ∪ the checked project's own
# venv bin. A project's enforcement.json can point tiers at any binary
# inside those roots — it cannot (a) resolve a binary from an untrusted
# PATH prefix, or (b) reach the app's interpreter/dependency set unless
# the project IS the app (venv-claim scan: dir/interpreter hops/pyvenv.cfg/
# site-packages, all realpath'd). Host-level attacks (a malicious HOME,
# a compromised system bin) are OUT of scope by this ruling.

from __future__ import annotations

import dataclasses
import fnmatch
import glob
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

# SPEC-09 SP0 (V3, pre-flight D2): the SPEC-09 worktree layout is
# <repo>/.worktrees/<agent_id> — git worktrees of the running app's repo,
# created by SP2's worktree_manager. Under the worktree phase, agents exec
# with project_path = the worktree, whose realpath differs from _APP_ROOT —
# the single-path identity compare would DENY a legitimate in-app worktree.
# _is_app_worktree() (below) is the explicit membership rule; SP2 wires the
# runtime to exec inside worktrees and builds on this contract — it must
# exist and be tested BEFORE SP2's integration. The worktrees parent is
# computed FROM _APP_ROOT at use time — no second cached global (a derived
# path constant diverges from its source when _APP_ROOT is patched or the
# checkout relocates; found by the SP0 V2×V3 composition test).


def _is_app_worktree(project_path: str) -> bool:
    """True when *project_path* is a git worktree OF the running app's repo.

    SPEC-09 SP0 (V3, pre-flight ruling D2): the ACTUAL rule is
    lexical/realpath membership — ``realpath(project_path)`` is a DIRECT
    CHILD of ``realpath(_APP_ROOT + "/.worktrees")``, and existence is
    NOT checked (a phantom child name passes by design;
    ``test_real_worktree_without_git_still_passes_gate`` pins it). The
    ``.worktrees`` parent must itself resolve under ``_APP_ROOT`` through
    its own realpath (deny-all when a symlinked ``.worktrees`` points
    outside the app). Consequence, pinned by ``test_d2``: a symlink
    PLANTED ELSEWHERE whose realpath lands inside ``.worktrees`` DOES
    confer membership — realpath collapsing means the link IS the member
    it points at. A symlink whose realpath lands elsewhere does not.

    SP2 wires the runtime to exec inside worktrees; this gate is the
    contract SP2 builds on — it must exist and be tested BEFORE SP2's
    integration. SP2 OBLIGATION (registered, re-audit BUG#14): worktree
    wiring sets project_path to the REAL worktree path — never a symlink
    into `.worktrees` (realpath would collapse the link INTO a member and
    confer membership on a path SP2 does not control).
    """
    if not project_path or not isinstance(project_path, str):
        return False
    real_child = os.path.realpath(os.path.abspath(project_path))
    real_app = os.path.realpath(_APP_ROOT)
    real_parent = os.path.realpath(os.path.join(_APP_ROOT, ".worktrees"))
    # SPEC-09 SP0 V3 no-symlink-escape rule: the .worktrees parent must
    # itself resolve UNDER the app root — a symlinked .worktrees pointing
    # outside the app (e.g. <app>/.worktrees → /tmp/evil) confers membership
    # on /tmp/evil/agent (probe: hole was OPEN before this check,
    # 2026-10-02). commonpath containment, fail-closed on ValueError.
    try:
        if os.path.commonpath([real_parent, real_app]) != real_app:
            logger.warning(
                "[enforcement] .worktrees resolves outside the app root — "
                "worktree gate denies all: parent=%s app=%s",
                real_parent, real_app,
            )
            return False
    except ValueError:
        return False
    # Direct-child membership on the REALPATH of both sides (basename
    # non-empty is implied: dirname(child) == parent means child != "/" —
    # and "/" cannot be a child of a .worktrees dir).
    return os.path.dirname(real_child) == real_parent


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
    """Return absolute path to venv Python interpreter, or None if refused/absent.

    Replaces the previous shell-sourcing behavior (which was a CRIT-2 RCE vector —
    a poisoned activate script would run on every enforcement check).
    Callers should substitute `python3 -m pytest` → `<result> -m pytest` when
    this returns a non-None value. (Phase 0 / CRIT-2)

    SP0 fix round (Debugger audit 2026-10-02): for a project that is NOT the
    app (and not an app worktree), a venv is refused when ANY of these
    surfaces resolves inside the app's own venv — each is a claim on the
    running app's interpreter/dependency set:

    1. the venv DIR's realpath (whole-.venv-dir symlink — V2 probe shape);
    2. any symlink HOP in bin/python's chain (BUG#2: realpath alone misses
       multi-hop links like bin/python → app venv bin/python → /usr/bin —
       the final realpath is outside the app venv while a hop still rides
       the app's files; replaced the dead literal clause-2, which compared
       a realpath'd LHS to a non-realpath'd RHS and never fired);
    3. pyvenv.cfg ``home``/``executable`` targets (realpath of the value,
       hops included — a home pointing into the app venv is the same claim);
    4. site-packages' realpath, located via the cfg ``version`` or a
       ``lib/python*/site-packages`` glob (BUG#1 false-PASS shape: foreign
       venv with its own pyvenv.cfg but site-packages symlinked into the
       app venv imports the app's dependency set while dir+python look
       native).

    Refusal is None + a warning naming the surface (fail-closed; the tests
    tier turns a refusal into a visible FAILED check via
    _venv_refusal_reason — SP0 fix round BUG#3).
    """
    venv_abs = os.path.join(project_path, venv_path)
    python_abs = os.path.join(venv_abs, "bin", "python")
    if not os.path.isfile(python_abs):
        return None

    claims = _venv_app_claims(project_path, venv_path)
    if claims:
        app_venv_dir = os.path.realpath(os.path.join(_APP_ROOT, ".venv"))
        surfaces = "; ".join(f"{surface} -> {evidence}" for surface, evidence in claims)
        logger.warning(
            "[enforcement] foreign project venv claims the app environment — "
            "refused: %s (app venv: %s; claims: %s)",
            python_abs, app_venv_dir, surfaces,
        )
        return None
    return python_abs


def _venv_refusal_reason(project_path: str, venv_path: str = ".venv") -> str | None:
    """Human-readable refusal reason when the project venv claims the app
    environment, None when the venv is absent, clean, or the project IS the
    app/worktree. SP0 fix round (BUG#3): lets _check_tests turn a refusal
    into a visible FAILED tier instead of a PATH-resolved fall-through.
    """
    claims = _venv_app_claims(project_path, venv_path)
    if not claims:
        return None
    return "; ".join(f"{surface} -> {evidence}" for surface, evidence in claims)


def _venv_app_claims(project_path: str, venv_path: str = ".venv") -> list[tuple[str, str]]:
    """Scan a project venv for surfaces claiming the running app's
    environment. Returns (surface, evidence) pairs; empty when none.

    A project that is NOT the app (and not an app worktree) claiming any
    of these surfaces is refused upstream (SP0 V2 + fix round, Debugger
    BUG#1/#2/#3):
    """
    venv_abs = os.path.join(project_path, venv_path)
    python_abs = os.path.join(venv_abs, "bin", "python")
    # SP0 fix round 2 (BUG#13): isfile() follows the symlink chain, so a
    # chain deeper than the kernel's ELOOP limit (~40) reads as "absent"
    # and the venv would be silently admitted as "no venv". An EXISTING
    # symlink at bin/python (lstat, no-follow) must be walked even when
    # the follow-stat fails — an unresolvable chain is precisely the
    # unverifiable case that must reach the (fail-closed) hop walk.
    if not os.path.isfile(python_abs) and not os.path.islink(python_abs):
        return []

    real_venv_dir = os.path.realpath(venv_abs)
    app_venv_dir = os.path.realpath(os.path.join(_APP_ROOT, ".venv"))
    project_is_app = (
        os.path.realpath(os.path.abspath(project_path)) == _APP_ROOT
        or _is_app_worktree(project_path)
    )
    if project_is_app:
        # The app (or a worktree of it) claiming its own venv is legitimate.
        return []

    claims: list[tuple[str, str]] = []
    # 1. dir-level containment (equal counts — whole-dir symlink probe).
    if _is_inside(real_venv_dir, app_venv_dir):
        claims.append(("venv dir", real_venv_dir))
    # 2. hop chain of bin/python (BUG#2 replacement for the dead clause).
    #    SP0 fix round 2 (BUG#13): a TRUNCATED chain is itself a claim —
    #    the tail is unproven, and silently dropping it could hide an
    #    app-venv hop past the cap (fail-closed, cap stays 40).
    hops, truncated = _symlink_chain(python_abs)
    if truncated:
        claims.append((
            "interpreter symlink chain",
            (
                f"exceeded 40 hops — treated as claiming the app environment "
                f"(fail-closed): {python_abs}"
            ),
        ))
    for hop in hops:
        if _is_inside(hop, app_venv_dir):
            # SP0 fix round 2 (BUG#12): truthful detail. A binary chain
            # ENTERING the app venv does not by itself prove the venv
            # claims the app ENVIRONMENT (prefix/site-packages may be
            # entirely local — e.g. a mirrored-interpreter layout); the
            # refusal stays (conservative, ruling 12) but must not assert
            # a claim that isn't there.
            claims.append((
                "interpreter symlink chain",
                (
                    f"enters the app venv ({hop}) — conservative refusal; "
                    f"if this venv is genuinely independent, give it its "
                    f"own interpreter binary"
                ),
            ))
    # 3. pyvenv.cfg home/executable targets (absolute or venv-relative).
    for key in ("home", "executable"):
        raw = _pyvenv_cfg_value(venv_abs, key)
        if not raw:
            continue
        target = raw if os.path.isabs(raw) else os.path.join(venv_abs, raw)
        # Literal-form containment FIRST: `executable = <app>/.venv/bin/python`
        # is a claim by its written path even when the file is a symlink
        # whose resolution lands outside (realpath collapses past the
        # literal address, and the hop walk starts by resolving it).
        literal = os.path.normpath(target)
        if _is_inside(literal, app_venv_dir):
            claims.append((f"pyvenv.cfg {key}", literal))
            continue
        real_target = os.path.realpath(target)
        if _is_inside(real_target, app_venv_dir):
            claims.append((f"pyvenv.cfg {key}", real_target))
            continue
        cfg_hops, cfg_truncated = _symlink_chain(target)
        if cfg_truncated:
            claims.append((
                f"pyvenv.cfg {key} (symlink chain)",
                (
                    f"exceeded 40 hops — treated as claiming the app "
                    f"environment (fail-closed): {target}"
                ),
            ))
            continue
        for hop in cfg_hops:
            if _is_inside(hop, app_venv_dir):
                claims.append((f"pyvenv.cfg {key} (symlink hop)", hop))
                break
    # 4. site-packages realpath (BUG#1: the dependency-set surface itself).
    #    SP0 fix round 2 (BUG#9+#11): EVERY candidate is checked — the
    #    single-path helper early-returned the FIRST isdir hit, so a decoy
    #    lib/python3.10/site-packages + a lying pyvenv.cfg version evaded
    #    the scan while the REAL (app-linked) site-packages sat under the
    #    cfg's claimed version. Enumerate lib AND lib64, dedup, check each.
    for site_packages in _venv_site_packages_all(venv_abs):
        if _is_inside(site_packages, app_venv_dir):
            claims.append(("site-packages", site_packages))
    return claims


def _symlink_chain(path: str) -> tuple[list[str], bool]:
    """Effective path after each SINGLE symlink hop along *path*'s chain.

    realpath() collapses the whole chain, so a multi-hop link like
    ``foreign/bin/python -> <app>/.venv/bin/python -> /usr/bin/python3``
    resolves OUTSIDE the app venv and a containment check on the final
    realpath misses the intermediate claim (Debugger BUG#2: the dead
    clause-2 tried to catch exactly this and never fired; the original
    implementation here made the same mistake — realpath per hop collapses
    the chain too). This walk replaces the deepest symlink component with
    its target, records the resulting path, and repeats until no component
    is a symlink (cycles break via a seen-set; a 40-hop cap backstops).
    Each recorded value is a claim surface for the caller's containment
    checks.

    Returns (hops, truncated). SP0 fix round 2 (BUG#13): ``truncated`` is
    True when the walk hit the 40-hop cap — the caller MUST treat that as
    a claim (fail-closed); a truncated chain is an unproven chain, and
    silently dropping the tail would convert an unverifiable venv into an
    admitted one (a deep link chain could hide an app-venv hop past the
    cap).
    """
    hops: list[str] = []
    current = os.path.abspath(path)
    seen: set[str] = {current}
    truncated = False
    while True:
        if len(hops) >= 40:
            truncated = True
            break
        link = None
        probe = current
        while True:
            parent, base = os.path.split(probe)
            if not base:
                break
            if os.path.islink(probe):
                link = probe
                break
            probe = parent
        if link is None:
            break
        try:
            target = os.readlink(link)
        except OSError as e:
            logger.debug("[enforcement] readlink failed on %s: %s", link, e)
            break
        target_abs = (
            target if os.path.isabs(target)
            else os.path.join(os.path.dirname(link), target)
        )
        current = os.path.normpath(
            os.path.join(target_abs, os.path.relpath(current, link))
        )
        if current in seen:
            break
        seen.add(current)
        hops.append(current)
    return hops, truncated


def _pyvenv_cfg_value(venv_abs: str, key: str) -> str | None:
    """Value of *key* in the venv's pyvenv.cfg, or None (missing/unreadable)."""
    cfg_path = os.path.join(venv_abs, "pyvenv.cfg")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip().lower() == key:
                    return v.strip()
    except OSError as e:
        logger.debug("[enforcement] pyvenv.cfg unreadable at %s: %s", cfg_path, e)
    return None


def _venv_site_packages_all(venv_abs: str) -> list[str]:
    """Realpaths of EVERY existing site-packages dir under the venv, dedup'd.

    SP0 fix round 2 (Debugger BUG#9 + BUG#11): the previous single-path
    helper early-returned the FIRST ``isdir`` hit, so a decoy
    ``lib/python3.10/site-packages`` (sorts before 3.12) plus a pyvenv.cfg
    lying about ``version`` evaded the scan while the REAL (app-linked)
    site-packages sat under the cfg's claimed version — probe: false-PASS
    with ``import nh3``. Only ``lib/`` was globbed, missing lib64 hosts.

    Strategy: glob ``lib*/python*/site-packages`` (lib AND lib64 bases)
    PLUS the pyvenv.cfg ``version`` candidate (a lying version must not
    HIDE the real one; the cfg candidate is also enumerated in case the
    glob misses a nonstandard layout). No early return — every candidate
    is realpath'd and returned for per-candidate containment checks.
    """
    candidates: list[str] = []
    # SP0 fix round 3 (BUG#15): venv_abs is PROJECT-SUPPLIED text
    # (enforcement.json venv_path) — unescaped, glob treats it as a PATTERN:
    # ``.venv[1]`` opens a character class, the scan returns [] and an
    # app-linked site-packages goes unseen (auditor probe: tier false-PASSED
    # with ``import nh3``). glob.escape the literal base; the
    # ``python*/site-packages`` tail stays a pattern. The cfg-``version``
    # candidate below is a literal join (no glob).
    candidates.extend(glob.glob(os.path.join(
        glob.escape(venv_abs), "lib", "python*", "site-packages"
    )))
    candidates.extend(glob.glob(os.path.join(
        glob.escape(venv_abs), "lib64", "python*", "site-packages"
    )))
    version = _pyvenv_cfg_value(venv_abs, "version")
    if version:
        parts = version.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            candidates.append(os.path.join(
                venv_abs, "lib", f"python{parts[0]}.{parts[1]}", "site-packages"
            ))
    seen: set[str] = set()
    realpaths: list[str] = []
    for candidate in candidates:
        if not os.path.isdir(candidate):
            continue
        real = os.path.realpath(candidate)
        if real not in seen:
            seen.add(real)
            realpaths.append(real)
    return realpaths


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

    # SPEC-09 SP0 (V1): resolved-binary gate — same refusal contract as the
    # tests/lint tiers (visible FAILED check, never a silent skip). The
    # gate's actual reason is embedded (fix round: "resolves into the app
    # environment" must reach the tier detail, not a hardcoded shadowing
    # message).
    allowed, gate_detail = _validate_resolved_binary(argv, project_path)
    if not allowed:
        logger.warning(
            "[enforcement] syntax tier refused: %s (argv=%r)", gate_detail, argv,
        )
        return EnforcementCheck(
            tier="syntax", tool="write_file", file=file_path,
            passed=False,
            detail=f"REFUSED: syntax binary — {gate_detail}",
            output="", duration_ms=0,
        )

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


# SPEC-09 SP0 (V1): resolved-binary ROOT allowlist — the second gate after
# the token allowlist (_ALLOWED_BINARIES, defense in depth). Verified live
# vector (probe, 2026-10-02): a `pytest` shim planted in any user-writable
# PATH dir executes with the scrubbed-but-real env because the token
# allowlist only checks the first TOKEN string, not the resolved binary
# path. After _parse_command_to_argv, argv[0] must resolve (shutil.which
# against the scrubbed PATH) inside one of these roots — realpath'd on both
# sides. (c) `<project>/.venv/bin` — the checked project's own venv — is
# appended per-project inside the validator (needs project_path).
_ALLOWED_BINARY_ROOTS: tuple[str, ...] = (
    "/usr/bin",
    "/usr/local/bin",
    "/bin",
    "/sbin",
    "/usr/sbin",
)


def _is_inside(realpath_child: str, realpath_parent: str) -> bool:
    """Realpath-both containment check (no ..-or-symlink escape).

    ValueError on different drives — unreachable on Linux, fail-closed.
    """
    try:
        return (
            os.path.commonpath([realpath_child, realpath_parent])
            == realpath_parent
        )
    except ValueError:
        return False


def _trusted_home_root() -> str | None:
    """Trusted-root candidate: realpath of the operator's HOME (BUG#4).

    The ruling treats the operator's home subtree as trusted user tooling
    territory (e.g. ``~/.local/bin`` pipx/rustup installs). Returns None
    (root absent → nothing admitted by it) when HOME resolves to ``/``
    (a universal root would allow everything — refuse rather than
    trust all) or when realpath raises.
    """
    try:
        home = os.path.realpath(os.path.expanduser("~"))
    except (OSError, RuntimeError) as e:
        logger.debug("[enforcement] HOME root unresolvable: %s", e)
        return None
    if home == "/":
        return None
    return home


def _resolve_binary_roots(project_path: str) -> tuple[str, ...]:
    """Allowed roots for resolved binaries, all realpath'd.

    SPEC-09 SP0 fix round (Debugger BUG#3): the running interpreter's dir
    is a trusted root ONLY when the checked project IS the running app
    (``_APP_ROOT``) or one of its git worktrees — a foreign project whose
    PATH resolves a binary into the app venv bin must NOT inherit the
    app's dependency set via this root (probe: refused-venv foreign
    project fell back to bare ``python3``, PATH resolved it into the app
    venv, realpath landed in /usr/bin → admitted via system root... the
    V2 refusal was silently bypassed). Trusted roots per the Supervisor
    ruling: system bins ∪ realpath(HOME) ∪ app-venv bin (app/worktree
    projects ONLY) ∪ the checked project's own venv bin.
    """
    roots = list(_ALLOWED_BINARY_ROOTS)
    home_root = _trusted_home_root()
    if home_root is not None:
        roots.append(home_root)
    project_is_app = bool(project_path) and (
        os.path.realpath(os.path.abspath(project_path)) == _APP_ROOT
        or _is_app_worktree(project_path)
    )
    if project_is_app:
        exe_dir = os.path.dirname(sys.executable or "")
        if exe_dir:
            roots.append(os.path.realpath(exe_dir))
    if project_path:
        # SP0 fix round 3 (BUG#16): root (c) must AGREE with the app-env
        # refusal's normalization — realpath the venv bin AND require the
        # (realpath'd) venv to sit LITERALLY inside the checked project.
        # A foreign ``.venv`` symlinked to the app venv realpaths OUT of
        # the project → NOT root (c) → the resolution reaches the (now
        # realpath'd) app-env refusal instead. App worktrees keep root (b)
        # (the running interpreter dir) and don't need (c).
        venv_bin_real = os.path.realpath(
            os.path.join(project_path, ".venv", "bin")
        )
        project_real = os.path.realpath(os.path.abspath(project_path))
        if (
            _is_inside(venv_bin_real, project_real)
            and venv_bin_real != project_real
        ):
            roots.append(venv_bin_real)
    return tuple(roots)


def _validate_resolved_binary(
    argv: list[str], project_path: str
) -> tuple[bool, str]:
    """V1 gate — resolve argv[0] against the scrubbed PATH and require it to
    live inside an allowed root (system bins, trusted HOME, running-interpreter
    dir (app projects only), or the checked project's venv bin).

    Returns (allowed, detail). Fail-closed: which() miss → refused with the
    token named; symlink-into-user-dir resolution → refused (realpath of the
    resolution must be inside a root, not the token's literal form).

    SP0 fix round (Debugger BUG#3, probe (a)): for a project that is NOT the
    app (and not an app worktree), a resolution INTO the app's own venv is
    refused outright — argv[0] inside the venv layout confers the app's
    interpreter + site-packages (sys.prefix is computed from argv[0]'s
    location), which is exactly the env-bleed the V2 venv gate refuses at
    detection time. This closes the fall-through: refused venv → bare
    ``python3`` → PATH hit <app>/.venv/bin/python3 → realpath /usr/bin →
    admitted via a system root → app dependency set ran the tests.

    The token allowlist (_validate_test_command) stays upstream as defense
    in depth — token first, then resolution.
    """
    if not argv:
        return False, "empty argv"
    token = argv[0]
    scrubbed_path = _get_scrubbed_env().get("PATH", "")
    resolved = shutil.which(token, path=scrubbed_path)
    if resolved is None:
        return False, (
            f"binary not found via scrubbed PATH: {token!r} — "
            "PATH shadowing refused"
        )
    app_venv_dir = os.path.realpath(os.path.join(_APP_ROOT, ".venv"))
    project_is_app = bool(project_path) and (
        os.path.realpath(os.path.abspath(project_path)) == _APP_ROOT
        or _is_app_worktree(project_path)
    )
    # SP0 fix round 3 (BUG#16): normalize BOTH sides. resolved may arrive
    # through a project symlink (``.venv`` → the app venv), so the literal
    # dirname sits OUTSIDE the app venv while the binary IS the app's —
    # realpath the dirname to match the realpath'd app_venv_dir (root (c)
    # is realpath'd for the same reason).
    # SP0 fix round 4 (BUG#18, auditor re-audit): dirname-realpath alone
    # misses the FILE-level symlink shape — a foreign .venv/bin/pytest that
    # symlinks directly to an app binary keeps a project-local dirname while
    # the file itself IS the app's. Check the FULL realpath too; either hit
    # (dir-level or file-level) refuses. The HOME root must never rescue an
    # app-venv path.
    if not project_is_app and (
        _is_inside(os.path.realpath(os.path.dirname(resolved)), app_venv_dir)
        or _is_inside(os.path.realpath(resolved), app_venv_dir)
    ):
        return False, (
            "binary resolves into the app environment (foreign project) — "
            f"env-bleed refused: {token!r} -> {resolved}"
        )
    real_resolved = os.path.realpath(resolved)
    # SP0 fix round 2 (BUG#10, Supervisor ruling 10): project-containment
    # OVERRIDES all trust roots. A resolved binary INSIDE the checked
    # project's realpath is refused unless it lives in that project's own
    # venv bin — otherwise the trusted HOME root admits a project-local
    # binary for the ~/projects/<repo> layout (probe: the auditor's own
    # repo shape — project under HOME, <proj>/tools/pytest admitted via
    # the HOME root). This rule runs FIRST; the roots check below is the
    # second layer. SP0 fix round 3 (BUG#16): the carve-out must AGREE
    # with _resolve_binary_roots' root (c) — realpath'd and only for a
    # venv bin literally inside the project (a symlinked foreign
    # ``.venv`` does not get the carve-out).
    project_real = os.path.realpath(os.path.abspath(project_path))
    carve_out_bin = os.path.realpath(
        os.path.join(project_real, ".venv", "bin")
    )
    carve_out = (
        _is_inside(carve_out_bin, project_real)
        and carve_out_bin != project_real
    )
    if (
        project_real != "/"
        and _is_inside(real_resolved, project_real)
        and not (carve_out and _is_inside(real_resolved, carve_out_bin))
    ):
        return False, (
            "binary resolves inside the checked project (outside its venv "
            f"bin) — project-supplied binaries are not trusted for "
            f"validation: {token!r} -> {real_resolved}"
        )
    roots = _resolve_binary_roots(project_path)
    for root in roots:
        if _is_inside(real_resolved, root):
            return True, resolved
    return False, (
        f"binary resolves outside allowed roots — PATH shadowing refused: "
        f"{token!r} -> {real_resolved} (allowed roots: {', '.join(roots)})"
    )


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
    # SPEC-09 SP0 (V3): worktree-aware identity gate (pre-flight D2). A git
    # worktree of the running app IS the app's source at a different path —
    # it gets the running interpreter. Anything else foreign does not.
    if (
        os.path.realpath(project_path) != _APP_ROOT
        and not _is_app_worktree(project_path)
    ):
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
    #
    # SP0 fix round (Debugger BUG#3): a REFUSED venv (claim on the app
    # environment) must NOT fall through to bare `python3` + PATH
    # resolution — probe (b): refused foreign project's `python3` resolved
    # via PATH into the app venv and the tier still PASSED. The refusal is
    # a visible FAILED tier naming the surface; no execution attempt. V2's
    # "refusal → tier skips as designed" note is superseded by this ruling.
    venv_refusal = _venv_refusal_reason(project_path, test_config.venv_path)
    if venv_refusal is not None:
        return EnforcementCheck(
            tier="tests", tool="write_file", file=file_path,
            passed=False,
            detail=(
                "test interpreter refused (venv validation): "
                f"{venv_refusal}"
            ),
            output="", duration_ms=0,
        )
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
            # SP0 fix round (BUG#6): the `command` field previously bypassed
            # the token allowlist entirely — full_suite_command was gated,
            # command was not — so `sh -c 'touch /tmp/marker'` parsed into
            # argv and EXECUTED (shell=False still runs argv[0]='sh', and
            # the resolved-binary gate rightly admits /usr/bin/sh; the token
            # allowlist is the only layer that refuses it). Same treatment
            # as the full_suite_command branches below.
            if not _validate_test_command(cmd_str):
                logger.warning(
                    "[enforcement] test command uses non-allowed binary: %s",
                    test_config.command,
                )
                return None
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
        run = _run_timed_command(argv, project_path, test_timeout)
        if run is None:
            # SPEC-09 SP0 (V1): resolved-binary gate refused argv[0] —
            # surface the refusal as a FAILED check, never a silent skip
            # (a silent skip would false-PASS the tier). Fix round: the
            # gate's actual reason is embedded (app-env bleed vs PATH
            # shadowing are different refusals with different remedies).
            _, gate_detail = _validate_resolved_binary(argv, project_path)
            return EnforcementCheck(
                tier="tests", tool="write_file", file=file_path,
                passed=False,
                detail=f"REFUSED: test binary — {gate_detail}",
                output="", duration_ms=0,
            )
        result, duration_ms = run
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


def _run_timed_command(argv: list[str], project_path: str, timeout: int) -> tuple[subprocess.CompletedProcess, int] | None:
    """Run a subprocess with argv list, shell=False, scrubbed env.

    Returns (result, duration_ms), or None when the V1 resolved-binary gate
    refuses argv[0] (SPEC-09 SP0: PATH-bleed — a shim planted in a
    user-writable PATH dir must never execute). Raises on timeout.
    CRIT-1/CRIT-2: shell=False is enforced. Env is scrubbed to PATH/HOME/LANG only. (Phase 0)
    """
    # SPEC-09 SP0 (V1): token allowlist upstream stays as defense in depth;
    # this is the resolution-side gate on the actual binary that would run.
    if not _validate_resolved_binary(argv, project_path)[0]:
        logger.warning(
            "[enforcement] subprocess refused by resolved-binary gate: argv=%r project=%s",
            argv, project_path,
        )
        return None
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

    # SPEC-09 SP0 (V1): resolved-binary gate BEFORE the subprocess —
    # refusal is a visible FAILED check (never silent, never a false pass).
    # Fix round: the gate's actual reason is embedded (same contract as the
    # syntax/tests tiers).
    allowed, gate_detail = _validate_resolved_binary(argv, project_path)
    if not allowed:
        logger.warning(
            "[enforcement] lint tier refused: %s (argv=%r)", gate_detail, argv,
        )
        return EnforcementCheck(
            tier="lint", tool="write_file", file=file_path,
            passed=False,
            detail=f"REFUSED: lint binary — {gate_detail}",
            output="", duration_ms=0,
        )

    try:
        run = _run_timed_command(argv, project_path, config.lint_timeout_seconds)
        if run is None:
            # Unreachable while the gate above holds; handled for type
            # honesty and defense-in-depth (same contract as the tests tier:
            # refusal is a visible FAILED check, never a silent skip).
            _, gate_detail = _validate_resolved_binary(argv, project_path)
            return EnforcementCheck(
                tier="lint", tool="write_file", file=file_path,
                passed=False,
                detail=f"REFUSED: lint binary — {gate_detail}",
                output="", duration_ms=0,
            )
        result, duration_ms = run
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
    # SP0 fix round (BUG#5): a REFUSED syntax check (resolved-binary gate)
    # is GATE-NEUTRAL for the downstream tiers, exactly like SKIPPED — the
    # syntax binary being untrusted says nothing about the tests/lint
    # binaries. Pre-fix, the refusal cascaded and silently disabled the
    # tests and lint tiers (visible as missing tiers in the check() output;
    # probe-proven in the audit).
    syntax_passed = all(
        c.tier != "syntax"
        or c.passed
        or c.detail.startswith("SKIPPED:")
        or c.detail.startswith("REFUSED:")
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