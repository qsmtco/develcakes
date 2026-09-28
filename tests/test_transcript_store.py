"""Tests for utils/transcript_store.py — SQLite+WAL transcript store core.

SPEC-08 SP1. All tests run on tmp_path DBs (or a monkeypatched config dir) —
no user state is ever touched. The file is bare-safe: no gi imports, runs
under `env -u DISPLAY`.

HIGH-3 guard (test_schema_has_no_secret_columns): the schema is pinned to an
exact column set so a future api_key/secret column cannot silently ship.
"""

import sqlite3
import threading

import pytest

from utils.transcript_store import TranscriptStore

# Exact schema contracts (HIGH-3): these sets are load-bearing. A new column
# must fail these tests and force a conscious spec-level decision.
TURN_COLUMNS = {
    "id",
    "session_key",
    "seq",
    "epoch",
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "tokens_used",
    "timestamp",
}
SESSION_COLUMNS = {
    "session_key",
    "agent_name",
    "model",
    "provider",
    "watermark",
    "epoch",
    "updated_at",
}


def _store(tmp_path) -> TranscriptStore:
    return TranscriptStore(db_path=str(tmp_path / "t.db"))


def test_append_and_load_all_roundtrip(tmp_path):
    """3 turns across roles; load_all returns 3 dicts, seqs 0,1,2, exact shape."""
    store = _store(tmp_path)
    store.append_turn("sk", "user", "hello")
    store.append_turn("sk", "assistant", "hi there", tokens_used=42)
    store.append_turn("sk", "tool", "result", tool_call_id="call_1")

    turns = store.load_all("sk")
    assert len(turns) == 3
    assert [t["seq"] for t in turns] == [0, 1, 2]
    assert [t["role"] for t in turns] == ["user", "assistant", "tool"]
    assert [t["content"] for t in turns] == ["hello", "hi there", "result"]
    assert turns[1]["tokens_used"] == 42
    assert turns[2]["tool_call_id"] == "call_1"
    # Exact dict shape: 9 keys, no id, tool_calls parsed-or-None.
    for turn in turns:
        assert set(turn.keys()) == {
            "session_key",
            "seq",
            "epoch",
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "tokens_used",
            "timestamp",
        }
        assert turn["session_key"] == "sk"
        assert turn["epoch"] == 0
        assert isinstance(turn["timestamp"], str)
        assert turn["timestamp"].endswith("Z")
    assert turns[0]["tool_calls"] is None


def test_tool_calls_roundtrip_persistence_shape(tmp_path):
    """tool_calls list of the persistence shape round-trips as a PARSED list."""
    store = _store(tmp_path)
    tool_calls = [
        {"call_id": "c1", "tool_name": "read_file", "arguments": {"path": "a.py"}},
        {"call_id": "c2", "tool_name": "write_file", "arguments": {"path": "b.py"}},
    ]
    store.append_turn("sk", "assistant", "working", tool_calls=tool_calls)

    turns = store.load_all("sk")
    assert len(turns) == 1
    got = turns[0]["tool_calls"]
    # Parsed list (not a JSON string), identical to what went in.
    assert isinstance(got, list)
    assert got == tool_calls


def test_tail_returns_last_n_ascending(tmp_path):
    """10 turns, tail(4) -> seqs 6..9 ascending."""
    store = _store(tmp_path)
    for i in range(10):
        store.append_turn("sk", "user", f"m{i}")

    tail = store.tail("sk", 4)
    assert [t["seq"] for t in tail] == [6, 7, 8, 9]
    assert [t["content"] for t in tail] == ["m6", "m7", "m8", "m9"]
    # tail must not consume or mutate the full history.
    assert len(store.load_all("sk")) == 10


def test_sessions_isolated(tmp_path):
    """Two keys interleaved; each load_all sees only its own turns."""
    store = _store(tmp_path)
    store.append_turn("a", "user", "a0")
    store.append_turn("b", "user", "b0")
    store.append_turn("a", "assistant", "a1")
    store.append_turn("b", "assistant", "b1")

    a = store.load_all("a")
    b = store.load_all("b")
    assert [t["content"] for t in a] == ["a0", "a1"]
    assert [t["content"] for t in b] == ["b0", "b1"]
    assert all(t["session_key"] == "a" for t in a)
    assert all(t["session_key"] == "b" for t in b)
    # Independent seq spaces.
    assert [t["seq"] for t in a] == [0, 1]
    assert [t["seq"] for t in b] == [0, 1]


def test_watermark_tracks_appends(tmp_path):
    """-1 before any append, then 0/1/2 as appends land."""
    store = _store(tmp_path)
    assert store.session_watermark("sk") == -1  # unknown session
    store.append_turn("sk", "user", "m0")
    assert store.session_watermark("sk") == 0
    store.append_turn("sk", "user", "m1")
    assert store.session_watermark("sk") == 1
    store.append_turn("sk", "user", "m2")
    assert store.session_watermark("sk") == 2


def test_delete_session_removes_all_rows(tmp_path):
    """D2: delete returns rows removed, wipes turns+sessions, idempotent on 2nd call."""
    store = _store(tmp_path)
    for i in range(3):
        store.append_turn("sk", "user", f"m{i}")

    assert store.delete_session("sk") == 3
    assert store.load_all("sk") == []
    assert store.session_watermark("sk") == -1  # sessions row gone too
    # Sad path: deleting an unknown (already-deleted) session is a no-op, not a raise.
    assert store.delete_session("sk") == 0
    # Unknown-from-the-start session also deletes 0.
    assert store.delete_session("never-existed") == 0


def test_bump_epoch_starts_new_seq(tmp_path):
    """D4: bump -> epoch 1; next append restarts seq at 0; load_all sees BOTH epochs ordered."""
    store = _store(tmp_path)
    store.append_turn("sk", "user", "e0-m0")
    store.append_turn("sk", "user", "e0-m1")
    assert store.session_watermark("sk") == 1

    new_epoch = store.bump_epoch("sk")
    assert new_epoch == 1
    # Watermark resets in the new epoch.
    assert store.session_watermark("sk") == -1

    seq = store.append_turn("sk", "user", "e1-m0")
    assert seq == 0  # per-epoch seq space restarts

    turns = store.load_all("sk")
    assert len(turns) == 3
    assert [(t["epoch"], t["seq"], t["content"]) for t in turns] == [
        (0, 0, "e0-m0"),
        (0, 1, "e0-m1"),
        (1, 0, "e1-m0"),
    ]
    # BUG #3 teeth: tail reads the CURRENT epoch ONLY — the pre-bump turns
    # must not leak into render hydration.
    tail_rows = store.tail("sk", 10)
    assert len(tail_rows) == 1
    assert tail_rows[0]["epoch"] == 1
    assert tail_rows[0]["seq"] == 0
    assert tail_rows[0]["content"] == "e1-m0"


def test_wal_mode_enabled(tmp_path):
    """PRAGMA journal_mode reports wal."""
    store = _store(tmp_path)
    row = store._conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_busy_timeout_set(tmp_path):
    """PRAGMA busy_timeout reports 5000 ms."""
    store = _store(tmp_path)
    row = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 5000


def test_close_idempotent(tmp_path):
    """close(); close() — second call is a no-op, no raise."""
    store = _store(tmp_path)
    store.append_turn("sk", "user", "m0")
    store.close()
    store.close()  # must not raise


def test_default_path_uses_config_dir(tmp_path, monkeypatch):
    """No db_path arg -> <config_dir>/transcript.db (via get_config_dir)."""
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
    store = TranscriptStore()
    store.append_turn("sk", "user", "m0")
    assert (tmp_path / "transcript.db").exists()


def test_schema_has_no_secret_columns(tmp_path):
    """HIGH-3: exact column sets on both tables; no secret-bearing columns.

    A literal "no column contains 'key'" is unsatisfiable — the mandated
    schema itself carries session_key. The teeth here are the exact-set
    assertion (any added column fails), plus: no 'api' substring anywhere,
    and 'key' is only tolerated in session_key.
    """
    store = _store(tmp_path)
    turn_cols = {
        r[1] for r in store._conn.execute("PRAGMA table_info(turns)").fetchall()
    }
    session_cols = {
        r[1] for r in store._conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    assert turn_cols == TURN_COLUMNS
    assert session_cols == SESSION_COLUMNS
    all_cols = turn_cols | session_cols
    assert not [c for c in all_cols if "api" in c.lower()]
    assert not [c for c in all_cols if "token_secret" in c.lower()]
    assert not [c for c in all_cols if "password" in c.lower()]
    key_bearing = [c for c in all_cols if "key" in c.lower()]
    assert key_bearing == ["session_key"]


def test_concurrent_append_two_threads_zero_lost(tmp_path):
    """Acceptance-core property, pinned early: 2 threads x 250 turns, distinct
    sessions, ONE shared store — all 500 present, per-session seqs contiguous."""
    store = _store(tmp_path)
    n_per_thread = 250
    barrier = threading.Barrier(2)

    def writer(session_key: str):
        barrier.wait()
        for i in range(n_per_thread):
            store.append_turn(session_key, "user", f"{session_key}-{i}")

    t1 = threading.Thread(target=writer, args=("alpha",))
    t2 = threading.Thread(target=writer, args=("beta",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    alpha = store.load_all("alpha")
    beta = store.load_all("beta")
    assert len(alpha) == n_per_thread
    assert len(beta) == n_per_thread
    assert [t["seq"] for t in alpha] == list(range(n_per_thread))
    assert [t["seq"] for t in beta] == list(range(n_per_thread))
    assert store.session_watermark("alpha") == n_per_thread - 1
    assert store.session_watermark("beta") == n_per_thread - 1


def test_two_instances_serialize_zero_lost(tmp_path):
    """BUG #1 pinned contract: TWO TranscriptStore instances on the SAME DB
    file, ONE session, 2 threads x 300 appends (thread->instance 1:1,
    Barrier start). Zero exceptions, 600/600 rows via a FRESH connection,
    seqs contiguous 0..599, watermark 599.

    This is the production shape — the global DB is opened per-consumer, so
    cross-INSTANCE serialization is the load-bearing property (BEGIN
    IMMEDIATE + busy_timeout; the second writer waits, never races).
    """
    db_path = str(tmp_path / "t.db")
    s1 = TranscriptStore(db_path=db_path)
    s2 = TranscriptStore(db_path=db_path)
    n_per_thread = 300
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def writer(store: TranscriptStore):
        try:
            barrier.wait()
            for i in range(n_per_thread):
                store.append_turn("sk", "user", f"turn-{i}")
        except (sqlite3.Error, RuntimeError, OSError) as exc:
            # Recorded, not swallowed — asserted empty below.
            errors.append(repr(exc))

    t1 = threading.Thread(target=writer, args=(s1,))
    t2 = threading.Thread(target=writer, args=(s2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    # FRESH third connection: verifies what actually hit the DB file, not
    # either instance's internal page cache view.
    fresh = TranscriptStore(db_path=db_path)
    rows = fresh.load_all("sk")
    assert len(rows) == 2 * n_per_thread
    assert sorted(t["seq"] for t in rows) == list(range(2 * n_per_thread))
    assert fresh.session_watermark("sk") == 2 * n_per_thread - 1
    fresh.close()
    s1.close()
    s2.close()


def test_two_instances_interleave_distinct_sessions(tmp_path):
    """BUG #1 companion: two instances, distinct sessions — each session's
    seq space stays contiguous under cross-instance contention."""
    db_path = str(tmp_path / "t.db")
    s1 = TranscriptStore(db_path=db_path)
    s2 = TranscriptStore(db_path=db_path)
    n_per_thread = 300
    barrier = threading.Barrier(2)

    def writer(store: TranscriptStore, session_key: str):
        barrier.wait()
        for i in range(n_per_thread):
            store.append_turn(session_key, "user", f"{session_key}-{i}")

    t1 = threading.Thread(target=writer, args=(s1, "alpha"))
    t2 = threading.Thread(target=writer, args=(s2, "beta"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    fresh = TranscriptStore(db_path=db_path)
    alpha = fresh.load_all("alpha")
    beta = fresh.load_all("beta")
    assert len(alpha) == n_per_thread
    assert len(beta) == n_per_thread
    assert [t["seq"] for t in alpha] == list(range(n_per_thread))
    assert [t["seq"] for t in beta] == list(range(n_per_thread))
    fresh.close()
    s1.close()
    s2.close()


def test_use_after_close_raises(tmp_path):
    """BUG #2 teeth: every method (except close itself) raises RuntimeError
    after close(); close's idempotence is asserted in the same test."""
    store = _store(tmp_path)
    store.append_turn("sk", "user", "m0")
    store.close()
    with pytest.raises(RuntimeError):
        store.append_turn("sk", "user", "m1")
    with pytest.raises(RuntimeError):
        store.tail("sk", 10)
    with pytest.raises(RuntimeError):
        store.load_all("sk")
    with pytest.raises(RuntimeError):
        store.delete_session("sk")
    with pytest.raises(RuntimeError):
        store.session_watermark("sk")
    with pytest.raises(RuntimeError):
        store.bump_epoch("sk")
    store.close()  # close-exempt: idempotent, must NOT raise


def test_failed_commit_leaves_no_trace(tmp_path):
    """BUG #4: a COMMIT that raises must not leave a dangling transaction —
    the half-applied turn must NEVER flush later, and the next append must
    allocate a clean seq (no double-write)."""
    store = _store(tmp_path)
    store.append_turn("sk", "user", "before")

    # sqlite3.Connection.execute is a read-only C attribute — the injection
    # wraps the connection at the seam the store itself uses (execute), so
    # the REAL failure path is exercised: the tx stays open on the underlying
    # connection after the failed COMMIT, and only the store's rollback
    # handler discards it.
    real_conn = store._conn

    class FlakyCommitConn:
        def __init__(self, real):
            self._real = real
            self._fail_next_commit = True

        def execute(self, sql, *args):
            if sql.strip().upper() == "COMMIT" and self._fail_next_commit:
                self._fail_next_commit = False
                raise sqlite3.OperationalError("injected")
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    store._conn = FlakyCommitConn(real_conn)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            store.append_turn("sk", "user", "must-not-persist")
    finally:
        store._conn = real_conn

    # Fresh connection on the same file: the failed turn is ABSENT — the
    # dangling transaction was rolled back, not left to flush later.
    fresh = TranscriptStore(db_path=str(tmp_path / "t.db"))
    rows = fresh.load_all("sk")
    assert [t["content"] for t in rows] == ["before"]
    fresh.close()

    # Next append after the failed one: clean seq allocation, no double-write.
    seq = store.append_turn("sk", "user", "after")
    assert seq == 1
    rows = store.load_all("sk")
    assert [t["content"] for t in rows] == ["before", "after"]
    assert [t["seq"] for t in rows] == [0, 1]
