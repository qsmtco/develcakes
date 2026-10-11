"""Bounded in-memory log of provider failures, plus one JSONL audit line each.

SPEC-22. Pure, thread-safe, no GTK. record() of the same object is a no-op.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque

from agent.llm.failure_classifier import ProviderFailure, sanitize_detail

_logger = logging.getLogger(__name__)

_CAP = 100
_lock = threading.Lock()
_events: deque[ProviderFailure] = deque(maxlen=_CAP)


def record(failure: ProviderFailure) -> None:
    """Store one failure. A second call with the same instance does nothing."""
    if not isinstance(failure, ProviderFailure):
        _logger.warning("provider_failure_store.record ignored non-failure %s", type(failure).__name__)
        return
    with _lock:
        # Flag on the instance, not id(). CPython reuses ids after GC, which
        # would drop a later, different failure.
        if getattr(failure, "_recorded", False):
            return
        failure._recorded = True  # type: ignore[attr-defined]
        _events.append(failure)
        snapshot = failure
    _append_audit(snapshot)


def recent(limit: int = 20, session_key: str | None = None) -> list[ProviderFailure]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        limit = 20
    with _lock:
        items = list(_events)
    if session_key is not None:
        items = [item for item in items if item.session_key == session_key]
    if limit == 0:
        return []
    return items[-limit:]


def summary() -> dict:
    with _lock:
        items = list(_events)
    cutoff = time.time() - 86400
    by_class: dict[str, int] = {}
    agents: list[str] = []
    seen_agents: set[str] = set()
    fresh = [item for item in items if isinstance(item.timestamp, (int, float)) and item.timestamp >= cutoff]
    for item in fresh:
        key = item.failure_class.value
        by_class[key] = by_class.get(key, 0) + 1
        if item.session_key and item.session_key not in seen_agents:
            seen_agents.add(item.session_key)
            agents.append(item.session_key)
    return {
        "last_24h": len(fresh),
        "by_class": by_class,
        "agents_affected": agents,
    }


def reset_for_tests() -> None:
    """Drop in-memory events. Tests only."""
    with _lock:
        _events.clear()


def _append_audit(failure: ProviderFailure) -> None:
    try:
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = {
            "kind": "provider_failure",
            "session_key": failure.session_key,
            "provider": failure.provider,
            "model": failure.model,
            "failure_class": failure.failure_class.value,
            "status_code": failure.status_code,
            "detail": sanitize_detail(failure.detail),
            "stage": failure.stage,
            "attempt": failure.attempt,
            "timestamp": failure.timestamp,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except Exception:
        _logger.exception("provider failure audit append failed")
