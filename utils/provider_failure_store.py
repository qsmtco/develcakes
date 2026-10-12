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

from agent.llm.failure_classifier import FailureClass, ProviderFailure, sanitize_detail

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
    if limit == 0:
        return []
    with _lock:
        items = list(_events)
    if items:
        # Warm path: the in-memory deque is authoritative.
        if session_key is not None:
            items = [item for item in items if item.session_key == session_key]
        return items[-limit:]
    # Cold path (SPEC-26): after a restart the deque is empty but
    # audit-log.jsonl is the durable record. Fall back to its tail so
    # recent() is not silently empty while durable records exist.
    rehydrated: list[ProviderFailure] = []
    for rec in _tail_audit_records():
        if rec.get("kind") != "provider_failure":
            continue
        ts = rec.get("timestamp")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        sc = rec.get("status_code")
        rehydrated.append(ProviderFailure(
            session_key=str(rec.get("session_key") or ""),
            provider=str(rec.get("provider") or ""),
            model=str(rec.get("model") or ""),
            failure_class=_coerce_failure_class(rec.get("failure_class")),
            status_code=sc if isinstance(sc, int) and not isinstance(sc, bool) else None,
            detail=sanitize_detail(str(rec.get("detail") or "")),
            stage=str(rec.get("stage") or ""),
            attempt=_coerce_attempt(rec.get("attempt")),
            timestamp=float(ts),
        ))
    if session_key is not None:
        rehydrated = [item for item in rehydrated if item.session_key == session_key]
    return rehydrated[-limit:]


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


# Note: a live FailureClass member degrades to UNKNOWN here because
# str(FailureClass.X) is "FailureClass.X" in Py3.12 — that path is unreachable
# via JSON, so don't "fix" it.
def _coerce_failure_class(value: object) -> FailureClass:
    """Map an on-disk failure_class back to a FailureClass member.

    Audit logs can carry class values written by older or newer builds, so an
    unrecognized value must not raise: the cold path degrades to UNKNOWN, the
    same fallback failure_classifier uses whenever a class can't be resolved.
    """
    try:
        return FailureClass(str(value or "unknown"))
    except ValueError:
        return FailureClass.UNKNOWN


def _coerce_attempt(value: object) -> int:
    """attempt is display-only on the rehydration path: ProviderFailure.attempt
    is written to the audit log verbatim and rehydrated verbatim; the only
    control-flow consumer of an attempt value is failure_policy.decide(), whose
    attempt parameter is supplied by runtime.py's chain-local counter and is
    never read from ProviderFailure. Negative values are preserved verbatim for
    fidelity. Non-integral on-disk values degrade to 0, never raise.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else 0
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


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
