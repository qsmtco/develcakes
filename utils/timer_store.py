# utils/timer_store.py — scheduled-timer persistence (SPEC-52).
#   Reads/Writes: <project>/.crabcakes/timers.json
#   Network: none   GTK: none   Imports: stdlib only (json, os, uuid, datetime)
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

REPEATS = ("", "daily")          # "" = one-shot
PROMPT_MAX = 2000                # chars
STALE_GRACE = 120                # seconds; one-shots past this are "expired"

_TIMERS_FILE = ".crabcakes/timers.json"


def _timers_path(project_path: str) -> str:
    return os.path.join(project_path, _TIMERS_FILE)


def load_timers(project_path: str) -> list[dict]:
    """Load pending timers. Missing/unreadable/corrupt JSON → []."""
    try:
        with open(_timers_path(project_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_timers(project_path: str, timers: list[dict]) -> bool:
    """Atomic write: tmp file + os.replace, os.makedirs(exist_ok=True).
    False on OSError."""
    tmp = None
    try:
        path = _timers_path(project_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(timers, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # best-effort cleanup — swallow secondary failure
        return False


def add_timer(
    project_path: str,
    *,
    target: str,
    prompt: str,
    fires_at: str,
    repeat: str = "",
) -> dict | None:
    """Validate and persist a timer; mint its id. None on invalid args or
    save failure."""
    if not isinstance(target, str) or not target.startswith("agent:"):
        return None
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_MAX:
        return None
    try:
        datetime.fromisoformat(fires_at)
    except (TypeError, ValueError):
        return None
    if repeat not in REPEATS:
        return None
    timer = {
        "id": uuid.uuid4().hex[:8],
        "target": target,
        "prompt": prompt.strip(),
        "fires_at": fires_at,
        "repeat": repeat,
    }
    timers = load_timers(project_path)
    timers.append(timer)
    if not save_timers(project_path, timers):
        return None
    return timer


def remove_timer(project_path: str, timer_id: str) -> bool:
    """Remove a timer by id. True when it existed and was removed."""
    timers = load_timers(project_path)
    remaining = [t for t in timers if t.get("id") != timer_id]
    if len(remaining) == len(timers):
        return False
    return save_timers(project_path, remaining)
