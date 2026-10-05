"""Tool execution audit trail.

Extracted from agent/runtime.py (Phase 5). Records tool name, args hash
(not raw args), approval decision, user identity, timestamp, and result
hash. In-memory by default; flushes to disk as JSON lines.

Pure Python — no GTK, no network, no agent.runtime imports.
"""

import hashlib
import json
import os
import threading
import time


class AuditEntry:
    """Single audit log entry for a tool execution."""
    __slots__ = ("tool_name", "args_hash", "approved", "user", "timestamp", "result_hash", "exit_code")

    def __init__(self, tool_name: str, args_hash: str, approved: bool | None,
                 user: str, timestamp: float, result_hash: str = "", exit_code: int | None = None):
        self.tool_name = tool_name
        self.args_hash = args_hash
        self.approved = approved
        self.user = user
        self.timestamp = timestamp
        self.result_hash = result_hash
        self.exit_code = exit_code


# Bug 3 (SPEC-AUDIT-CLEANUP-1): FIFO cap on the in-memory entry buffer.
# A long session previously grew _entries unbounded; the runtime now also
# auto-flushes at every terminal turn (see agent/runtime.py _terminate_turn).
MAX_ENTRIES = 2000


class AuditLog:
    """In-memory audit log for tool executions (A-4).

    Defense-in-depth: records tool name, args hash (not raw args),
    approval decision, user identity, timestamp, and result hash.
    In-memory by default; flush to disk via flush_audit_log().

    Bug 3 (SPEC-AUDIT-CLEANUP-1):
      * FIFO-capped at MAX_ENTRIES — record() drops the oldest entries
        beyond the cap, so a long session no longer grows memory unbounded.
      * Auto-flushed at every terminal turn by agent/runtime.py
        `_terminate_turn` (try/except there — an audit flush must never
        break the turn). flush_audit_log() clears the buffer after a
        successful write.
    """

    def __init__(self):
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def record(self, tool_name: str, args: dict, approved: bool | None,
               user: str, result: str = "", exit_code: int | None = None) -> None:
        """Record a tool execution in the audit log (capped at MAX_ENTRIES)."""
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:16]
        result_hash = hashlib.sha256(result.encode()).hexdigest()[:16] if result else ""
        entry = AuditEntry(
            tool_name=tool_name,
            args_hash=args_hash,
            approved=approved,
            user=user,
            timestamp=time.time(),
            result_hash=result_hash,
            exit_code=exit_code,
        )
        with self._lock:
            self._entries.append(entry)
            excess = len(self._entries) - MAX_ENTRIES
            if excess > 0:
                del self._entries[:excess]

    def flush_audit_log(self, path: str | None = None) -> str | None:
        """Flush audit log to disk as JSON lines.

        Args:
            path: Output file path. Defaults to ~/.config/develcakes/audit-log.jsonl.

        Returns:
            The file path written, or None if no entries.
        """
        from utils.config import get_config_dir
        if path is None:
            path = os.path.join(get_config_dir(), "audit-log.jsonl")
        with self._lock:
            if not self._entries:
                return None
            entries = list(self._entries)
            self._entries.clear()
        with open(path, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps({
                    "tool_name": e.tool_name,
                    "args_hash": e.args_hash,
                    "approved": e.approved,
                    "user": e.user,
                    "timestamp": e.timestamp,
                    "result_hash": e.result_hash,
                    "exit_code": e.exit_code,
                }) + "\n")
        return path

    @property
    def entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)