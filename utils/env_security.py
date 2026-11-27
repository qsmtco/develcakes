# utils/env_security.py
# Centralized env scrubbing for subprocess execution.
#
# MED-2 / CRIT-2 / Phase 0 follow-up: subprocesses (whether shell tools or
# post-write enforcement) must NOT inherit API keys, gateway tokens, or other
# sensitive env vars. The allowlist is intentionally narrow:
#   - PATH, HOME, LANG, LC_ALL, LANGUAGES, TZ, TMPDIR, PWD
#   - PYTEST_DISABLE_PLUGIN_AUTOLOAD (CONSOLIDATED audit Fix 3)
#
# Anything not on the allowlist is dropped. This includes:
#   - Provider API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
#   - Gateway tokens / device-auth
#   - User shell customizations that might leak secrets
#   - The code-executing vars: PYTEST_ADDOPTS, PYTHONPATH, PYTHONSTARTUP,
#     NODE_OPTIONS, MYPY_CONFIG_FILE, RUFF_CONFIG, GOFLAGS, GIT_CONFIG_*
#   - Anything the user happens to have in their environment
#
# The allowlist is the same one enforcement.py used previously (Phase 0).
# Moved here so agent.tools._exec_command (MED-2) can share it without
# creating a dependency cycle between tools.py and enforcement.py.

from __future__ import annotations

import os


# Forwardable env vars for subprocesses. Add new entries with caution —
# anything on this list will be visible to shell tools and enforcement.
ALLOWED_SUBPROCESS_ENV_VARS: frozenset[str] = frozenset({
    "PATH",      # needed for command lookup
    "HOME",      # needed for ~ expansion in some commands
    "LANG",      # locale-aware tools
    "LC_ALL",    # locale
    "LANGUAGES", # locale
    "TZ",        # timezone-aware tools
    "TMPDIR",    # temp file locations
    "PWD",       # shell prompts / some scripts
    # CONSOLIDATED audit Fix 3 (C2): a SUPPRESSION variable, not a loader.
    # pytest reads it itself and can only REFUSE to auto-load setuptools
    # entry-point plugins — it cannot add one. Allowing it is strictly
    # narrowing, which is why it belongs here while PYTEST_ADDOPTS (which
    # injects arbitrary argv, `-p evilplugin` included) must stay out.
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
})


def get_scrubbed_env() -> dict[str, str]:
    """Return a minimal env dict for subprocesses (MED-2 / CRIT-2).

    Includes only safe vars from ALLOWED_SUBPROCESS_ENV_VARS. All provider API
    keys, gateway tokens, and other sensitive env vars are stripped.
    """
    return {k: v for k, v in os.environ.items() if k in ALLOWED_SUBPROCESS_ENV_VARS}
