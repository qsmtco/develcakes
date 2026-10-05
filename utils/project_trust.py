# utils/project_trust.py
# HIGH-5: Per-project trust gate for `.crabcakes/` rule/bug ingestion.
#
# When the user opens a project that contains a `.crabcakes/` directory
# with rule/bug files, those files are injected into the system prompt
# wrapped in <untrusted-project-data> fences (HIGH-5 partial, Phase 0).
# The fence is necessary-but-not-sufficient: a determined attacker can
# still try to manipulate the model through the fenced content.
#
# Defense-in-depth: gate `.crabcakes/` ingestion behind a per-project
# trust prompt on FIRST open. After the user approves once, the project
# path is recorded in the trust store and subsequent loads skip the gate.
# If the user denies, the project's `.crabcakes/` content is silently
# skipped for the session (and for future sessions, until they approve).
#
# Storage: `~/.config/develcakes/trusted_projects.json` (or the platform-
# equivalent via utils.config.get_config_dir()). The file holds a mapping
# from absolute project path to {trusted: bool, ts: ISO8601 timestamp,
# reason: str}. Reverse mapping is not needed.
#
# Thread-safety: read-mostly. Writes go through a lock to avoid two
# concurrent first-opens both prompting.

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable

from utils.config import get_config_dir

logger = logging.getLogger(__name__)


_TRUST_FILE = "trusted_projects.json"
_LOCK = threading.Lock()


def _trust_path() -> str:
    """Return the absolute path to the trust store JSON file."""
    return os.path.join(get_config_dir(), _TRUST_FILE)


def _read_trust() -> dict[str, dict]:
    """Read the trust store. Returns {} on missing/corrupt file."""
    path = _trust_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Trust store at %s is not a dict; ignoring", path)
            return {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read trust store at %s: %s", path, e)
        return {}


def _write_trust(data: dict[str, dict]) -> None:
    """Write the trust store atomically. Caller holds _LOCK."""
    path = _trust_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as e:
        logger.error("Failed to write trust store at %s: %s", path, e)


def is_project_trusted(project_path: str) -> bool:
    """Return True if `project_path` is in the trust store with trusted=True."""
    if not project_path:
        return False
    abs_path = os.path.abspath(project_path)
    with _LOCK:
        data = _read_trust()
    entry = data.get(abs_path)
    return bool(entry and entry.get("trusted") is True)


def trust_project(project_path: str, reason: str = "user-approved") -> None:
    """Mark `project_path` as trusted. Persists to the trust store."""
    if not project_path:
        return
    abs_path = os.path.abspath(project_path)
    with _LOCK:
        data = _read_trust()
        data[abs_path] = {
            "trusted": True,
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }
        _write_trust(data)
    logger.info("HIGH-5: trusted project %s (%s)", abs_path, reason)


def untrust_project(project_path: str) -> None:
    """Remove `project_path` from the trust store."""
    if not project_path:
        return
    abs_path = os.path.abspath(project_path)
    with _LOCK:
        data = _read_trust()
        if abs_path in data:
            del data[abs_path]
            _write_trust(data)
    logger.info("HIGH-5: untrusted project %s", abs_path)


def has_crabcakes_content(project_path: str) -> bool:
    """Return True if `project_path/.crabcakes/` has any loadable rule/bug files.

    Used by callers to decide whether the trust gate should fire. Only
    triggers when there's actual content to load — projects without a
    `.crabcakes/` directory are unaffected.
    """
    if not project_path:
        return False
    crabcakes_dir = os.path.join(project_path, ".crabcakes")
    if not os.path.isdir(crabcakes_dir):
        return False
    # Check for at least one .md file (the bug-journal / rules files
    # are the ones that get ingested via _load_project_context_file).
    try:
        for name in os.listdir(crabcakes_dir):
            if name.endswith("-bugs.md") or name.endswith("-rules.md"):
                full = os.path.join(crabcakes_dir, name)
                if os.path.isfile(full) and os.path.getsize(full) > 0:
                    return True
    except OSError:
        return False
    return False


# Type for the prompt callback the UI registers. The callback should
# display a confirmation dialog to the user and return True if they
# approved trust, False otherwise. The callback runs on the UI thread.
TrustPromptCallback = Callable[[str], bool]

_prompt_callback: TrustPromptCallback | None = None


def set_trust_prompt_callback(cb: TrustPromptCallback | None) -> None:
    """Register (or clear) the UI callback that prompts the user to trust a project.

    The callback receives the absolute project path and should return True
    (user approved) or False (user denied). If no callback is registered
    when a trust decision is needed, the default behavior is to deny
    (skip ingestion) — fail-secure.
    """
    global _prompt_callback
    _prompt_callback = cb


def request_trust_if_needed(project_path: str) -> bool:
    """HIGH-5 trust gate. Returns True if `.crabcakes/` ingestion may proceed.

    Logic:
      - If the project has no `.crabcakes/` content, return True (nothing to gate).
      - If the project is already trusted, return True.
      - Otherwise, call the registered UI callback. If it returns True,
        record the trust and return True. If False (or no callback), return
        False (skip ingestion for this session).
    """
    if not project_path:
        return False
    abs_path = os.path.abspath(project_path)

    if not has_crabcakes_content(abs_path):
        return True  # nothing to gate

    if is_project_trusted(abs_path):
        return True

    # Need user input. If no callback is registered, fail-secure (skip).
    cb = _prompt_callback
    if cb is None:
        logger.warning(
            "HIGH-5: project %s has .crabcakes/ content but no trust "
            "callback is registered; skipping ingestion",
            abs_path,
        )
        return False

    approved = bool(cb(abs_path))
    if approved:
        trust_project(abs_path, reason="user-approved-via-callback")
    else:
        logger.info(
            "HIGH-5: user declined to trust project %s; "
            ".crabcakes/ ingestion skipped for this session",
            abs_path,
        )
    return approved
