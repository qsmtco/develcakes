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
# Tail-scan bounds for the cold-read fallback. Both are module-level so tests
# can monkeypatch them to force multi-chunk scans and prove the byte cap.
_TAIL_READ_CHUNK = 64 * 1024
_TAIL_MAX_SCAN_BYTES = 8 * 1024 * 1024
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


def _byte_before(handle, offset: int) -> bytes:
    """Return the single byte preceding ``offset`` in an open binary file.

    Used to tell a clean line boundary (previous byte is a newline) from a
    split line (previous byte is line content) without reading the whole
    chunk. Returns b"" at offset 0, which callers must treat as "no byte
    before this point" rather than a content byte.
    """
    if offset <= 0:
        return b""
    handle.seek(offset - 1)
    return handle.read(1)


def _tail_audit_records(max_records: int = _CAP) -> list[dict]:
    """Read the last ``max_records`` provider_failure records from the audit log.

    Cold-restart fallback for ``recent()``/``summary()`` (SPEC-22 §"Failure
    store replay after restart", SPEC-28). Bounded read from the tail, never
    raises — a missing or unreadable log yields [].

    The bound is on RECORDS, not on raw lines. audit-log.jsonl is shared with
    other writers (agent_call etc.), so a byte/line window taken before the
    kind filter would let unrelated traffic evict provider failures from the
    window (SPEC-28). Instead the file is scanned backward from EOF in
    _TAIL_READ_CHUNK chunks, newest line first, keeping provider_failure dicts
    until ``max_records`` are collected.

    Guarantees:
    - Record-bound: exactly the last ``max_records`` provider_failure records,
      unrelated lines never consume a slot.
    - Bounded scan: at most _TAIL_MAX_SCAN_BYTES is read, so the cost is O(cap)
      and never O(file). Reaching the cap mid-file is a truncation, not an
      error — the returned window is still correct, just shorter.
    - Fragment-safe: a line straddling a chunk boundary is never parsed on its
      own; its leading fragment is carried into the next (older) chunk and the
      line is parsed only once it is whole. An unterminated trailing line is
      only parsed if the chunk reaches offset 0.
    - Returns records in file order (oldest -> newest), truncated to the last
      ``max_records``. Malformed lines are skipped individually.
    """
    try:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records <= 0:
            return []
        path = _audit_log_path()
        if not os.path.exists(path):
            return []
        records: list[dict] = []
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            scanned = 0
            fragment = b""  # leading bytes of a line split by a chunk edge
            while end > 0 and len(records) < max_records and scanned < _TAIL_MAX_SCAN_BYTES:
                size = min(_TAIL_READ_CHUNK, end, _TAIL_MAX_SCAN_BYTES - scanned)
                if size <= 0:
                    break
                end -= size
                handle.seek(end)
                chunk = handle.read(size)
                # FILE order: this chunk is NEWER than the carry, and the
                # carry holds the tail of the line whose head sits in this
                # chunk, so the carried bytes follow the bytes just read.
                buffer = chunk + fragment
                scanned += size
                # A line is split across this edge when older bytes remain and
                # the byte immediately before the chunk is not a newline.
                split_head = end > 0 and _byte_before(handle, end) != b"\n"
                lines = buffer.split(b"\n")
                if split_head:
                    # Oldest piece is a head with no tail yet: defer it to the
                    # next-older chunk, never parse it standalone and never drop
                    # it, so the spanning record rehydrates exactly once.
                    fragment = lines.pop(0)
                else:
                    # Clean edge (or the file's first line): nothing to defer.
                    fragment = b""
                if lines and lines[-1] == b"":
                    lines.pop()  # trailing newline -> empty tail element

                for raw in reversed(lines):  # newest -> oldest
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw.decode("utf-8", errors="replace"))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(rec, dict) and rec.get("kind") == "provider_failure":
                        records.append(rec)
                        if len(records) >= max_records:
                            break
        records.reverse()  # newest-first collection -> file order
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
