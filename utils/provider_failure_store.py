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
    cutoff = time.time() - 86400
    with _lock:
        items = list(_events)
    if items:
        # Warm path: the in-memory deque is authoritative.
        pairs = [
            (item.failure_class.value, item.session_key, item.timestamp)
            for item in items
            if isinstance(item.timestamp, (int, float))
        ]
    else:
        # Cold path (SPEC-22 fix-round BUG #7): after a restart the deque is
        # empty but audit-log.jsonl is the durable record. Fall back to its
        # tail so "is anything failing right now?" is not silently green.
        pairs = []
        for rec in _tail_audit_records():
            ts = rec.get("timestamp")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                pairs.append((
                    str(rec.get("failure_class") or ""),
                    str(rec.get("session_key") or ""),
                    float(ts),
                ))
    by_class: dict[str, int] = {}
    agents: list[str] = []
    seen_agents: set[str] = set()
    fresh = [pair for pair in pairs if pair[2] >= cutoff]
    for class_value, session_key, _ts in fresh:
        key = class_value or "unknown"
        by_class[key] = by_class.get(key, 0) + 1
        if session_key and session_key not in seen_agents:
            seen_agents.add(session_key)
            agents.append(session_key)
    return {
        "last_24h": len(fresh),
        "by_class": by_class,
        "agents_affected": agents,
    }


def reset_for_tests() -> None:
    """Drop in-memory events. Tests only."""
    with _lock:
        _events.clear()


def _audit_log_path() -> str:
    from utils.config import get_config_dir
    return os.path.join(get_config_dir(), "audit-log.jsonl")


def _tail_audit_records(max_lines: int = _CAP) -> list[dict]:
    """Read the last ``max_lines`` provider_failure records from the audit log.

    Cold-restart fallback for ``summary()`` (SPEC-22 §"Failure store replay
    after restart"). Bounded read from the tail, never raises — a missing or
    unreadable log yields [].
    """
    try:
        path = _audit_log_path()
        if not os.path.exists(path):
            return []
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # Read a bounded tail window; one JSONL line is well under 1 KiB,
            # so _CAP * 1024 covers max_lines records with margin.
            window = min(size, max_lines * 1024)
            handle.seek(size - window)
            chunk = handle.read()
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        records: list[dict] = []
        for line in lines[-max_lines:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(rec, dict) and rec.get("kind") == "provider_failure":
                records.append(rec)
        return records
    except Exception:
        _logger.exception("provider failure audit tail read failed")
        return []


def _append_audit(failure: ProviderFailure) -> None:
    try:
        path = _audit_log_path()
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
