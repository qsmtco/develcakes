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
import time

# NOTE on CHECK(seq >= 0): CREATE TABLE IF NOT EXISTS does not ALTER existing
# databases — the CHECK only guards FRESH DBs (it IntegrityErrors a negative
# INSERT at the door). Pre-existing DBs keep their old table definition; the
# query-level `seq >= 0` floor in covers() is the real fix for them.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK(seq >= 0),
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
    diverged INTEGER NOT NULL DEFAULT 0,
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
        self._closed = False
        self._perms_set = False
        # BUG#5 (SP2 fix round): a failed constructor must not orphan the
        # connection — close it before re-raising, or every failed init leaks
        # one fd + one sqlite handle for the life of the process.
        try:
            # BUG #5 (SP1 audit register): busy_timeout does NOT gate the
            # journal_mode=WAL transition — the pragma returns SQLITE_BUSY on a
            # concurrent first open (4/60 threads, 14/40 processes measured).
            # Bounded retry: 5 attempts x 10 ms cleared every failure in testing
            # (0/160) while keeping the wait far below any UI-visible threshold.
            self._wal_retry(5, 0.010)
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(_SCHEMA)
        except BaseException:
            self._conn.close()
            raise
        # BUG#6 (SP2 fix round): transcript data is as sensitive as the JSON
        # conversation files (HIGH-3 parity) — 0600 on the db, 0700 on the
        # config dir, and 0600 on the WAL sidecars once they exist (sidecars
        # appear on first WRITE and inherit the umask; see
        # _ensure_sidecar_perms for the one-shot post-first-append chmod).
        self._apply_perms()

    def _wal_retry(self, attempts: int, delay: float) -> None:
        """Set journal_mode=WAL with bounded retry (BUG #5 first-open race)."""
        for attempt in range(attempts):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)

    def _apply_perms(self) -> None:
        """BUG#6: 0600 on the db + any EXISTING sidecars, 0700 on the parent
        config dir. Best-effort: OSError is swallowed (non-POSIX filesystems,
        read-only dirs) — a perms failure must never break construction.
        """
        for path in (self._db_path, self._db_path + "-wal", self._db_path + "-shm"):
            try:
                if os.path.exists(path):
                    os.chmod(path, 0o600)
            except OSError:
                pass
        parent = os.path.dirname(self._db_path)
        if parent:
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass

    def _ensure_sidecar_perms(self) -> None:
        """BUG#6 one-shot: chmod -wal/-shm to 0600 after the first append.

        WAL sidecars are created by the first write and inherit the process
        umask, so init-time perms cannot cover them. Guarded by a flag — this
        runs exactly once per instance (a live instance never loses its
        sidecars: sqlite deletes them only when the LAST connection closes
        cleanly, and per-instance close() is terminal).
        """
        if self._perms_set:
            return
        self._perms_set = True
        for suffix in ("-wal", "-shm"):
            try:
                path = self._db_path + suffix
                if os.path.exists(path):
                    os.chmod(path, 0o600)
            except OSError:
                pass

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
        seq: int | None = None,
    ) -> int:
        """Append one turn in the session's CURRENT epoch. Returns its seq.

        seq = MAX(seq)+1 within (session_key, epoch), allocated inside an
        IMMEDIATE transaction — the DB write lock is held across the
        SELECT+INSERT, so concurrent writers (same instance, or across
        instances/processes on the same DB file) serialize instead of racing.
        Also upserts the sessions row (watermark, updated_at). Thread-safe.

        Explicit ``seq`` (SP2 dual-write): writes the row at that exact seq —
        UNIQUE(session_key, epoch, seq) turns a duplicate into IntegrityError,
        never silent duplication — and the sessions watermark is still upserted
        to it, so the next delta-append starts past it automatically. Do not
        mix explicit-seq and auto-seq appends within one epoch, or seq order
        diverges from JSON index order.
        """
        with self._lock:
            self._ensure_open()
            try:
                self._conn.execute(self._BEGIN)
                epoch = self._conn.execute(
                    f"SELECT {_CUR_EPOCH}", (session_key,)
                ).fetchone()[0]
                if seq is None:
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
            self._ensure_sidecar_perms()
            # Narrow for the return type: the tx guarantees seq is set by
            # here (auto path derives it inside the lock; the explicit path
            # was canonicalized above). An assert, not int() — int(None)
            # would be a silent type lie; the assert makes the invariant
            # explicit and lets pyright narrow int | None -> int.
            assert seq is not None
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

    def append_delta(self, session_key: str, base_idx: int, rows: list[dict]) -> int:
        """Atomically append an index-aligned run of turns past the watermark.

        One lock acquisition + one BEGIN IMMEDIATE: reads wm INSIDE the tx,
        starts at max(wm + 1, base_idx), and writes rows[i] at seq =
        base_idx + i for every index >= start — indexes the tx finds already
        committed (a racing writer's rows) are SKIPPED, never re-written.
        Upserts watermark to the last seq it actually wrote and commits. Two
        racing savers serialize here; the second re-derives from the first's
        COMMITTED wm — the union of both deltas survives.

        rows: list of dicts with keys role, content, tool_calls, tool_call_id,
        tokens_used (the persistence shape; NO api_key — HIGH-3). rows[0] sits
        at JSON index ``base_idx`` — callers must trim accordingly.
        Returns the last seq written, or -1 if nothing to append.

        Committing per the watermark ruling: append_delta upserts wm ONLY to a
        seq it actually wrote this tx — wm never moves ahead of the rows that
        back it.
        """
        with self._lock:
            self._ensure_open()
            if not rows:
                return -1
            try:
                self._conn.execute(self._BEGIN)
                epoch = self._conn.execute(
                    f"SELECT {_CUR_EPOCH}", (session_key,)
                ).fetchone()[0]
                # In-tx wm read: a racing writer's COMMITTED watermark wins —
                # this tx starts past it, so both deltas land (union).
                committed_wm = self._conn.execute(
                    "SELECT watermark FROM sessions WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                committed_wm = committed_wm[0] if committed_wm else -1
                start = max(committed_wm + 1, base_idx)
                # rows[i] sits at JSON index base_idx + i (wrapper contract:
                # rows is trimmed so rows[0] == messages[base_idx]). Indexes
                # below `start` were committed by a racing writer — SKIP them
                # (never re-write the prefix at shifted seqs).
                last_seq: int | None = None
                for i, row in enumerate(rows):
                    seq = base_idx + i
                    if seq < start:
                        continue
                    self._conn.execute(
                        "INSERT INTO turns (session_key, seq, epoch, role,"
                        " content, tool_calls, tool_call_id, tokens_used,"
                        f" timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {_TS_NOW})",
                        (
                            session_key,
                            seq,
                            epoch,
                            str(row.get("role", "")),
                            str(row.get("content", "")),
                            json.dumps(row["tool_calls"])
                            if row.get("tool_calls")
                            else None,
                            row.get("tool_call_id"),
                            row.get("tokens_used") or 0,
                        ),
                    )
                    last_seq = seq
                if last_seq is not None:
                    self._conn.execute(
                        "INSERT INTO sessions (session_key, watermark, epoch,"
                        f" updated_at) VALUES (?, ?, ?, {_TS_NOW})"
                        " ON CONFLICT(session_key) DO UPDATE SET"
                        " watermark = excluded.watermark,"
                        " updated_at = excluded.updated_at",
                        (session_key, last_seq, epoch),
                    )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                self._rollback_quietly()
                raise
            except Exception:
                self._rollback_quietly()
                raise
            self._ensure_sidecar_perms()
            # -1 sentinel (not None) on full-skip: the return type is int —
            # this tx wrote nothing because a racing writer already committed
            # every index in range.
            return last_seq if last_seq is not None else -1

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

    def covers(self, session_key: str, upto: int) -> bool:
        """True iff the CURRENT epoch holds a row at every seq in [0..upto].

        Coverage, not count (SP3 audit BUG#1): cardinality matches while
        indexes are missing (phantom high-seq rows) and load_all() spans all
        epochs (prior-epoch rows inflate the count). One SQL COUNT against
        the current epoch with seqs constrained to [0..upto] and UNIQUE
        distinctness ⟺ exact coverage of every index in [0..upto].

        The `seq >= 0` floor is LOAD-BEARING (round-3 fix): without it a
        negative-seq row (corrupt DB / manual edit — exactly the threat
        model) satisfies count==upto+1 while an in-range index is absent —
        e.g. {0,1,-1} counts 3 and "covers" 0..2 with index 2 missing.

        `upto < 0` → True vacuously (the empty range [0..-1] needs no rows).

        Params bind session_key TWICE: the embedded _CUR_EPOCH subquery
        carries its own ? placeholder (same pattern as tail()/row_at()).
        """
        if upto < 0:
            return True
        with self._lock:
            self._ensure_open()
            n = self._conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_key = ?"
                f" AND epoch = {_CUR_EPOCH} AND seq >= 0 AND seq <= ?",
                (session_key, session_key, upto),
            ).fetchone()[0]
        return n == upto + 1

    # DELETED in the SP2 fix round (BUG#2/#3) — sync_watermark had no
    # replacement, and none is allowed by the ruling: the sessions watermark
    # is a DERIVED fact ("appended through", never "acknowledged through").
    # It is written only alongside the rows that back it (append_turn /
    # append_delta inside BEGIN IMMEDIATE). The JSON-only-restart case needs
    # no sync: the next save's append_delta backfills from wm=-1, which IS
    # D3's gradual self-migration.

    # ── internals ───────────────────────────────────────────────────────────

    def row_at(self, session_key: str, seq: int) -> dict | None:
        """The row at (session_key, seq) in the CURRENT epoch — the guard's
        anchor probe. Read-only; _row_to_dict shape or None if absent.

        Params bind session_key TWICE: the embedded _CUR_EPOCH subquery
        carries its own ? placeholder (same pattern as tail()).
        """
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                f"SELECT {_TURN_COLS} FROM turns WHERE session_key = ?"
                f" AND epoch = {_CUR_EPOCH} AND seq = ?",
                (session_key, session_key, seq),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def mark_diverged(self, session_key: str) -> None:
        """Set diverged=1 (creates the sessions row if absent). Idempotent.

        The append-only guard's flag: once set, the wrapper suspends the store
        delta entirely (early return; JSON stays authoritative and the store's
        existing rows are kept as the append-only audit ledger — NEVER deleted
        or rebuilt, per the trim ruling).
        """
        with self._lock:
            self._ensure_open()
            try:
                self._conn.execute(self._BEGIN)
                self._conn.execute(
                    "INSERT INTO sessions (session_key, diverged, updated_at)"
                    f" VALUES (?, 1, {_TS_NOW})"
                    " ON CONFLICT(session_key) DO UPDATE SET"
                    " diverged = 1, updated_at = excluded.updated_at",
                    (session_key,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise

    def is_diverged(self, session_key: str) -> bool:
        """Whether the append-only guard has flagged this session."""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT diverged FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            return bool(row[0]) if row else False

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
