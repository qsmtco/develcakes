"""SQLite+WAL transcript store — the append-safe replacement for per-session
whole-file JSON rewrites.

SPEC-08 SP1. Kills the last-writer-wins class: turns are appended as individual
rows and seq numbers are allocated transactionally, so two concurrent writers
can never clobber each other's turns.

**Single-writer discipline lives HERE**: within one process via the instance
lock (``self._lock`` serializes access to the connection); ACROSS processes and
instances via ``BEGIN IMMEDIATE`` + ``busy_timeout`` — the write lock is
acquired UP FRONT, so a second writer on the same DB file waits for the first's
COMMIT instead of racing the seq SELECT. Nothing else in the codebase touches
the DB directly — everything flows through the public methods.

Layer rules (architecture): pure Python + sqlite3 stdlib. NO GTK, NO network,
NO agent/ imports — this module sits BELOW the agent layer.

D1=(c): the store is global per install — default DB path is
``<config_dir>/transcript.db`` (via ``get_config_dir()``), because sessions are
global-by-session-key and may migrate across projects. SPEC-11's rename moves
it with the config dir. A per-project DB would split one session's history.

HIGH-3: the store never accepts or stores api_key — the schema (test-pinned in
tests/test_transcript_store.py::test_schema_has_no_secret_columns) has no
secret-bearing columns, and the API surface has no such parameter.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,
    tool_call_id TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL,
    UNIQUE(session_key, epoch, seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_key, epoch, seq);
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT,
    watermark INTEGER NOT NULL DEFAULT -1,
    epoch INTEGER  NOT NULL DEFAULT 0,
    updated_at TEXT
);
"""

# Explicit column list (never SELECT *): row unpacking stays order-robust even
# if the schema ever grows, and _row_to_dict's contract is pinned by tests.
_TURN_COLS = (
    "session_key, seq, epoch, role, content, tool_calls, tool_call_id,"
    " tokens_used, timestamp"
)

# SQLite strftime('%f') yields SS.mmm (two sub-second digits); ISO-8601 allows
# 1-6 fraction digits and Python 3.11+ fromisoformat parses both, so no
# normalization is needed for round-tripping.
_TS_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

# Current-epoch resolution: an unknown session (no sessions row yet) is in
# epoch 0. COALESCE keeps the very first append from inserting a NULL epoch.
_CUR_EPOCH = "COALESCE((SELECT epoch FROM sessions WHERE session_key = ?), 0)"


class TranscriptStore:
    """Append-safe SQLite transcript store (WAL, thread-safe, global per install)."""

    def __init__(self, db_path: str | None = None) -> None:
        # Call-time import + default resolution: tests monkeypatch
        # utils.config.get_config_dir; a module-top import would freeze the
        # unpatched function into this module's namespace.
        if db_path is None:
            from utils.config import get_config_dir

            db_path = os.path.join(get_config_dir(), "transcript.db")
        self._db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        # isolation_level=None (autocommit): ALL transaction control is
        # explicit. With the default isolation_level, sqlite3 opens an
        # implicit DEFERRED tx at first DML — two instances could both run
        # their seq SELECT before either held the write lock (the audit's
        # BUG #1 lost-turn race). BEGIN IMMEDIATE below closes that window.
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._closed = False

    # ── write path ──────────────────────────────────────────────────────────

    # Explicit BEGIN IMMEDIATE on every write path (BUG #1 fix): the DB-level
    # write lock is acquired BEFORE the seq SELECT, so a second instance on
    # the same DB file waits (busy_timeout) instead of racing the allocation.
    _BEGIN = "BEGIN IMMEDIATE"

    def append_turn(
        self,
        session_key: str,
        role: str,
        content: str,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        tokens_used: int = 0,
    ) -> int:
        """Append one turn in the session's CURRENT epoch. Returns its seq.

        seq = MAX(seq)+1 within (session_key, epoch), allocated inside an
        IMMEDIATE transaction — the DB write lock is held across the
        SELECT+INSERT, so concurrent writers (same instance, or across
        instances/processes on the same DB file) serialize instead of racing.
        Also upserts the sessions row (watermark, updated_at). Thread-safe.
        """
        with self._lock:
            self._ensure_open()
            try:
                self._conn.execute(self._BEGIN)
                epoch = self._conn.execute(
                    f"SELECT {_CUR_EPOCH}", (session_key,)
                ).fetchone()[0]
                seq = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 FROM turns"
                    " WHERE session_key = ? AND epoch = ?",
                    (session_key, epoch),
                ).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO turns (session_key, seq, epoch, role, content,"
                    " tool_calls, tool_call_id, tokens_used, timestamp)"
                    f" VALUES (?, ?, ?, ?, ?, ?, ?, ?, {_TS_NOW})",
                    (
                        session_key,
                        seq,
                        epoch,
                        role,
                        content,
                        json.dumps(tool_calls) if tool_calls else None,
                        tool_call_id,
                        tokens_used,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO sessions (session_key, watermark, epoch, updated_at)"
                    f" VALUES (?, ?, ?, {_TS_NOW})"
                    " ON CONFLICT(session_key) DO UPDATE SET"
                    " watermark = excluded.watermark, updated_at = excluded.updated_at",
                    (session_key, seq, epoch),
                )
                self._conn.execute("COMMIT")
            except Exception:
                # BUG #4: never leave a dangling transaction — a half-applied
                # write would flush on a later statement and block other
                # writers for the full busy_timeout.
                self._rollback_quietly()
                raise
            return seq

    def delete_session(self, session_key: str) -> int:
        """D2: delete ALL turns (every epoch) + the sessions row. Returns the
        number of turn rows removed. Committing; idempotent (unknown key -> 0)."""
        with self._lock:
            self._ensure_open()
            try:
                self._conn.execute(self._BEGIN)
                cur = self._conn.execute(
                    "DELETE FROM turns WHERE session_key = ?", (session_key,)
                )
                deleted = cur.rowcount
                self._conn.execute(
                    "DELETE FROM sessions WHERE session_key = ?", (session_key,)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
            return deleted

    def bump_epoch(self, session_key: str) -> int:
        """D4: bump the session's epoch and reset its watermark to -1. Returns
        the new epoch. Post-MVP trigger (/clear); exercised by tests now.

        Note: bumping an unknown session creates the sessions row (epoch 1,
        watermark -1) so the next append lands in the new epoch.
        """
        with self._lock:
            self._ensure_open()
            try:
                self._conn.execute(self._BEGIN)
                self._conn.execute(
                    "INSERT INTO sessions (session_key, epoch, watermark)"
                    " VALUES (?, 1, -1)"
                    " ON CONFLICT(session_key) DO UPDATE SET"
                    " epoch = epoch + 1, watermark = -1",
                    (session_key,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
            return self._conn.execute(
                "SELECT epoch FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()[0]

    def close(self) -> None:
        """Commit + close. Idempotent — a second call is a no-op.

        The bare commit stays (harmless in autocommit mode — nothing pending;
        the original SP1 contract is preserved)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.commit()
            self._conn.close()

    # ── read path ───────────────────────────────────────────────────────────

    def tail(self, session_key: str, n: int = 200) -> list[dict]:
        """Most-recent n turns of the CURRENT epoch, ascending. Render hydration."""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"SELECT {_TURN_COLS} FROM (SELECT {_TURN_COLS} FROM turns"
                f" WHERE session_key = ? AND epoch = {_CUR_EPOCH}"
                " ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC",
                (session_key, session_key, n),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def load_all(self, session_key: str) -> list[dict]:
        """All turns across ALL epochs, ascending by (epoch, seq). Context rebuild."""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"SELECT {_TURN_COLS} FROM turns WHERE session_key = ?"
                " ORDER BY epoch ASC, seq ASC",
                (session_key,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def session_watermark(self, session_key: str) -> int:
        """Watermark (last appended seq) of the CURRENT epoch; -1 if unknown."""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT watermark FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            return row[0] if row else -1

    # ── internals ───────────────────────────────────────────────────────────

    def _rollback_quietly(self) -> None:
        """Roll back a failed transaction without masking the real error.

        Guarded: if BEGIN IMMEDIATE itself failed to acquire the lock, there
        is no active transaction and rollback() would raise (belt-and-braces
        per fix-round instructions point 5).
        """
        try:
            self._conn.rollback()
        except sqlite3.OperationalError:
            pass

    def _ensure_open(self) -> None:
        """Raise a clear error on use-after-close instead of a bare sqlite3 error."""
        if self._closed:
            raise RuntimeError(f"TranscriptStore is closed (db_path={self._db_path!r})")

    def _row_to_dict(self, row: tuple) -> dict:
        """Row -> public dict shape (9 keys, no id; tool_calls parsed-or-None)."""
        (
            session_key,
            seq,
            epoch,
            role,
            content,
            tool_calls_json,
            tool_call_id,
            tokens_used,
            timestamp,
        ) = row
        return {
            "session_key": session_key,
            "seq": seq,
            "epoch": epoch,
            "role": role,
            "content": content,
            "tool_calls": json.loads(tool_calls_json) if tool_calls_json else None,
            "tool_call_id": tool_call_id,
            "tokens_used": tokens_used,
            "timestamp": timestamp,
        }
