"""Tests for utils/transcript_store.py — SQLite+WAL transcript store core.

SPEC-08 SP1. All tests run on tmp_path DBs (or a monkeypatched config dir) —
no user state is ever touched. The file is bare-safe: no gi imports, runs
under `env -u DISPLAY`.

HIGH-3 guard (test_schema_has_no_secret_columns): the schema is pinned to an
exact column set so a future api_key/secret column cannot silently ship.
"""

import os
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
    "diverged",
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
    """PRAGMA busy_timeout reports 15000 ms.

    SP3 fix round 2: bumped from 5000 after the re-audit's load probe —
    load-only starvation hit at exactly 5.006s under host IO contention
    (5s expiry), and busy_timeout=15000 measured 6/6 clean under deliberate
    parallel load. Worst-case stall only matters under pathological
    contention; real writes are ms."""
    store = _store(tmp_path)
    row = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 15000


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

    SP2 fix-round-2 sanctioned exception: sessions gained `diverged` (8
    columns now) — a non-secret metadata flag for the append-only guard
    (front-trimmed sessions go JSON-only; the store rows stay as audit
    ledger). The pin was NOT weakened: the set is still exact, and the
    secret-substring sweeps below still run over it.
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
    """SP1-audit rescope: two instances on one DB file with distinct sessions —
    each session's seq space stays CONTIGUOUS under cross-instance contention.

    This pins per-session seq isolation, NOT the BUG#1 lost-turn race (pre-fix
    detection here measured 0/5 — distinct sessions never contend for seq
    allocation). The BUG#1 race pin is the SAME-session test above
    (test_two_instances_serialize_zero_lost).
    """
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
    with pytest.raises(RuntimeError):
        # SP2: the atomic delta path is inside the same closed-connection guard.
        store.append_delta("sk", 0, [{"role": "user", "content": "m1"}])
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


def test_concurrent_first_open_wal_race(tmp_path):
    """BUG #5 (SP1 register): journal_mode=WAL is NOT gated by busy_timeout —
    concurrent first opens can race the journal-mode transition. With the
    bounded retry (5 x 10ms), 4 threads racing TranscriptStore() on ONE db
    file must ALL succeed (pre-fix failure rate: 4/60 threads, 14/40 runs).
    """
    db_path = str(tmp_path / "t.db")
    n_threads = 4
    barrier = threading.Barrier(n_threads)
    errors: list[str] = []
    stores: list[TranscriptStore] = []

    def opener():
        try:
            barrier.wait()
            stores.append(TranscriptStore(db_path=db_path))
        except (sqlite3.Error, OSError) as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=opener) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"first-open failures: {errors}"
    assert len(stores) == n_threads
    # Every surviving store is usable AND in WAL mode — the pragma landed.
    for s in stores:
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"journal_mode={mode!r} (retry abandoned the transition)"
        s.append_turn("sk", "user", "post-race")
        s.close()
    fresh = TranscriptStore(db_path=db_path)
    assert len(fresh.load_all("sk")) == n_threads
    fresh.close()


def _flaky_conn(real_conn, fail_sqls: set[str], max_failures: int = 1):
    """Delegating connection that raises OperationalError on the FIRST N
    executions of any statement whose normalized SQL is in fail_sqls — the
    same execute-seam injection as test_failed_commit_leaves_no_trace."""

    class FlakyConn:
        def __init__(self):
            self._fail_budget = dict.fromkeys(fail_sqls, max_failures)

        def execute(self, sql, *args):
            key = sql.strip().upper()
            for pattern, budget in self._fail_budget.items():
                if budget > 0 and pattern in key:
                    self._fail_budget[pattern] = budget - 1
                    raise sqlite3.OperationalError(f"injected: {pattern}")
            return real_conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    return FlakyConn()


def test_delete_and_bump_epoch_rollback_injection(tmp_path):
    """MF4 (SP1 register): failure injection on BOTH remaining write paths —
    delete_session and bump_epoch must (a) propagate the error, (b) leave NO
    dangling transaction (a half-applied tx would flush on a later statement
    and block other writers for the full busy_timeout), and (c) leave the
    connection usable (clean next write, consistent state).
    """
    store = _store(tmp_path)
    store.append_turn("sk", "user", "keep-0")
    store.append_turn("sk", "user", "keep-1")
    real_conn = store._conn

    # ── delete_session: DELETE FROM TURNS fails mid-tx ──
    store._conn = _flaky_conn(real_conn, {"DELETE FROM TURNS"})
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            store.delete_session("sk")
    finally:
        store._conn = real_conn

    # (b) no dangling tx + (c) data intact: everything the failed tx touched
    # is still committed-prior state — the turn rows survived, watermark too.
    rows = store.load_all("sk")
    assert [t["content"] for t in rows] == ["keep-0", "keep-1"]
    assert store.session_watermark("sk") == 1

    # (c) connection recovered: a REAL delete now succeeds atomically.
    assert store.delete_session("sk") == 2
    assert store.load_all("sk") == []
    assert store.session_watermark("sk") == -1

    # Re-seed, then ── bump_epoch: the epoch upsert fails mid-tx ──
    store.append_turn("sk", "user", "reseed")
    store._conn = _flaky_conn(real_conn, {"INSERT INTO SESSIONS"})
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            store.bump_epoch("sk")
    finally:
        store._conn = real_conn

    # (b) no dangling tx: watermark must NOT have reset to -1 (the sessions
    # upsert rolled back), and the reseed turn must still be there.
    assert store.session_watermark("sk") == 0
    assert [t["content"] for t in store.load_all("sk")] == ["reseed"]

    # (c) connection recovered: a REAL bump lands and the next append opens
    # the new epoch cleanly.
    assert store.bump_epoch("sk") == 1
    seq = store.append_turn("sk", "user", "new-epoch")
    assert seq == 0
    assert store.session_watermark("sk") == 0
    turns = store.load_all("sk")
    assert [(t["epoch"], t["seq"], t["content"]) for t in turns] == [
        (0, 0, "reseed"),
        (1, 0, "new-epoch"),
    ]


def test_watermark_equals_max_row_seq(tmp_path):
    """BUG#2/#3 invariant pin (SP2 fix round): the watermark is a DERIVED
    fact — it equals max(seq) of the CURRENT epoch's rows, or -1 when there
    are none. Checked after EVERY wrapper-style operation sequence (append
    xk, delete, append again, plus delta + bump interleave), via a fresh raw
    SQL query that bypasses the store's own accounting.

    Must be able to fail: any future API that lets wm drift ahead of the rows
    (the deleted sync_watermark's sin) dies here.
    """
    store = _store(tmp_path)

    def assert_invariant(session_key: str) -> None:
        row = store._conn.execute(
            "SELECT MAX(seq) FROM turns WHERE session_key = ? AND epoch = 0",
            (session_key,),
        ).fetchone()
        max_seq = row[0] if row and row[0] is not None else -1
        assert store.session_watermark(session_key) == max_seq, (
            f"watermark {store.session_watermark(session_key)} != max(seq) "
            f"{max_seq} for {session_key!r} — wm drifted ahead of the rows"
        )

    sk = "sk"
    assert_invariant(sk)  # empty store: -1 == -1
    for i in range(3):
        store.append_turn(sk, "user", f"m{i}")
        assert_invariant(sk)
    last = store.append_delta(
        sk, 3, [{"role": "user", "content": "d3"}, {"role": "user", "content": "d4"}]
    )
    assert last == 4
    assert_invariant(sk)
    store.delete_session(sk)
    assert_invariant(sk)  # no rows again: wm must be -1, not 4
    store.append_turn(sk, "user", "fresh")
    assert_invariant(sk)  # re-appended at seq 0, not 5
    store.bump_epoch(sk)
    # Post-bump the CURRENT epoch has no rows — wm must reset with them.
    assert store.session_watermark(sk) == -1


def test_db_files_0600(tmp_path):
    """BUG#6: db + WAL sidecars are 0600 after init + first append; the
    config dir is 0700. Sidecars are asserted only if present (platform
    tolerance); the db is asserted unconditionally."""
    import stat

    db_path = tmp_path / "t.db"
    store = TranscriptStore(db_path=str(db_path))
    store.append_turn("sk", "user", "m0")  # creates the sidecars

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = tmp_path / f"t.db{suffix}"
        if sidecar.exists():  # absent on some platforms/instants — tolerated
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600, suffix
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    store.close()


def test_failed_init_closes_connection(tmp_path):
    """BUG#5 (SP2): a constructor that fails AFTER connect must close the
    connection before re-raising — 50 attempts leak ZERO fds (gc disabled
    during the loop so no finalizer masks the leak)."""
    import gc

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database at all" * 32)

    def _open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    before = _open_fds()
    gc.disable()
    try:
        for _ in range(50):
            with pytest.raises(sqlite3.DatabaseError):
                TranscriptStore(db_path=str(garbage))
    finally:
        gc.enable()
    after = _open_fds()
    assert after <= before, f"fd leak across 50 failed inits: {before} -> {after}"


def test_append_delta_full_skip_returns_neg_one(tmp_path):
    """BUG#1 (round-2): an append_delta whose EVERY index was already
    committed by a racing writer returns the -1 sentinel — never None (the
    declared return type is int; None leaked through the full-skip path)."""
    store = _store(tmp_path)
    rows = [{"role": "user", "content": f"m{i}"} for i in range(3)]
    assert store.append_delta("sk", 0, rows) == 2
    # Same range again: everything already committed -> full skip.
    assert store.append_delta("sk", 0, rows) == -1
    # Rows and watermark untouched by the skipped tx.
    turns = store.load_all("sk")
    assert [t["content"] for t in turns] == ["m0", "m1", "m2"]
    assert store.session_watermark("sk") == 2


def test_mark_diverged_flag_lifecycle(tmp_path):
    """diverged flag: False before any mark, True after (idempotent), and
    cleared by delete_session — a deleted session starts fresh."""
    store = _store(tmp_path)
    assert store.is_diverged("sk") is False  # unknown session
    store.append_turn("sk", "user", "m0")
    assert store.is_diverged("sk") is False  # appends do NOT flag
    store.mark_diverged("sk")
    assert store.is_diverged("sk") is True
    store.mark_diverged("sk")  # idempotent
    assert store.is_diverged("sk") is True
    # Marking a never-appended session also works (creates the row).
    store.mark_diverged("fresh")
    assert store.is_diverged("fresh") is True
    # delete_session clears the flag with the rest of the session state.
    store.delete_session("sk")
    assert store.is_diverged("sk") is False


def test_row_at_anchor_probe(tmp_path):
    """row_at: the guard's anchor read — (role, content, ...) at a seq in the
    CURRENT epoch, _row_to_dict shape, None when absent."""
    store = _store(tmp_path)
    store.append_turn("sk", "user", "m0")
    store.append_turn("sk", "assistant", "m1", tool_call_id="c9")

    anchor = store.row_at("sk", 0)
    assert anchor is not None
    assert anchor["role"] == "user"
    assert anchor["content"] == "m0"
    mid = store.row_at("sk", 1)
    assert mid is not None
    assert mid["tool_call_id"] == "c9"
    assert store.row_at("sk", 99) is None
    assert store.row_at("never-appended", 0) is None


def test_covers_exact_unity_count_is_coverage(tmp_path):
    """covers(): True iff the current epoch holds a row at every seq in
    [0..upto] — count against seq<=upto == upto+1 (UNIQUE makes seqs
    distinct, so equality ⟺ no gaps)."""
    store = _store(tmp_path)
    assert store.covers("sk", -1) is True      # vacuous: empty range
    assert store.covers("sk", 0) is False      # no rows at all
    store.append_turn("sk", "user", "m0")
    assert store.covers("sk", 0) is True
    assert store.covers("sk", 1) is False      # gap: index 1 missing
    store.append_turn("sk", "assistant", "m1")
    assert store.covers("sk", 1) is True
    # Phantom high-seq row: count inflates, coverage does not.
    store.append_turn("sk", "user", "phantom", seq=9)
    assert store.covers("sk", 2) is False      # index 2 still missing
    assert store.covers("sk", 1) is True       # [0..1] unaffected by phantom


def test_covers_current_epoch_only(tmp_path):
    """covers() reads the CURRENT epoch — prior-epoch rows never satisfy it
    (the multi-epoch inflation probe)."""
    store = _store(tmp_path)
    for i in range(5):
        store.append_turn("sk", "user", f"e0-{i}")
    assert store.covers("sk", 4) is True
    store.bump_epoch("sk")
    # Current (epoch-1) holds nothing: coverage resets.
    assert store.covers("sk", 0) is False
    assert store.covers("sk", 4) is False
    store.append_turn("sk", "user", "e1-0")
    store.append_turn("sk", "user", "e1-1")
    assert store.covers("sk", 1) is True
    assert store.covers("sk", 2) is False      # epoch-0's 5 rows don't count
    assert len(store.load_all("sk")) == 7      # load_all spans all epochs


def test_covers_unknown_session_and_use_after_close(tmp_path):
    """covers(): unknown session → False for any upto >= 0; closed store
    raises the clear use-after-close error (same guard as every read)."""
    store = _store(tmp_path)
    assert store.covers("never-appended", 0) is False
    store.close()
    try:
        store.covers("sk", 0)
    except RuntimeError as exc:
        assert "TranscriptStore is closed" in str(exc)
    else:
        raise AssertionError("covers() after close must raise RuntimeError")


def test_covers_negative_seq_row_is_not_coverage(tmp_path):
    """Round-3 BUG#1: covers() needs the `seq >= 0` floor — a negative-seq
    row (corrupt DB / manual edit, exactly the threat model) must not
    satisfy the count.

    Built on a hand-rolled PRE-CHECK-SCHEMA table: CHECK(seq >= 0) only
    guards FRESH DBs (CREATE TABLE IF NOT EXISTS never ALTERs), so a
    pre-existing DB still accepts the poison row — the query floor is what
    protects it. Seeds {0,1,-1}: covers(sk, 2) is False (index 2 absent,
    the -1 row must not count), covers(sk, 1) stays True."""
    import sqlite3 as _sq

    db_path = str(tmp_path / "legacy.db")
    legacy = _sq.connect(db_path)
    legacy.execute(
        "CREATE TABLE turns ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_key TEXT NOT NULL, seq INTEGER NOT NULL,"
        " epoch INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL,"
        " content TEXT NOT NULL DEFAULT '', tool_calls TEXT,"
        " tool_call_id TEXT, tokens_used INTEGER NOT NULL DEFAULT 0,"
        " timestamp TEXT NOT NULL, UNIQUE(session_key, epoch, seq))"
    )
    for seq, content in ((0, "m0"), (1, "m1"), (-1, "poison")):
        legacy.execute(
            "INSERT INTO turns (session_key, seq, epoch, role, content,"
            " tool_calls, tool_call_id, tokens_used, timestamp)"
            " VALUES ('sk', ?, 0, 'user', ?, NULL, NULL, 0,"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (seq, content),
        )
    legacy.commit()
    legacy.close()

    # IF NOT EXISTS keeps the legacy table (no CHECK) — the threat scenario.
    store = TranscriptStore(db_path=db_path)

    assert store.covers("sk", 2) is False  # count 3 == upto+1, but poison
    assert store.covers("sk", 1) is True   # real rows unaffected
    assert store.covers("sk", 0) is True


def test_schema_rejects_negative_seq_on_fresh_db(tmp_path):
    """The CHECK(seq >= 0) belt: a fresh DB IntegrityErrors a negative-seq
    INSERT at the door. (Pre-existing DBs don't get the CHECK — IF NOT
    EXISTS never ALTERs — which is why covers() carries its own floor.)"""
    store = _store(tmp_path)
    import sqlite3 as _sq

    try:
        store._conn.execute(
            "INSERT INTO turns (session_key, seq, epoch, role, content,"
            " tool_calls, tool_call_id, tokens_used, timestamp)"
            " VALUES ('sk', -1, 0, 'user', 'x', NULL, NULL, 0,"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )
    except _sq.IntegrityError as exc:
        assert "seq" in str(exc)
    else:
        raise AssertionError("CHECK(seq >= 0) must refuse a negative-seq INSERT")
