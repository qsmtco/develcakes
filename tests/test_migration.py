"""SPEC-08 SP3 — JSON→store migration + concurrency acceptance tests.

Covers migrate_conversations_to_store (idempotent + resumable + per-session
error isolation + COUNT-based skip + diverged-kept-on-JSON), the
run_store_migration_once launch gate (banner fires ONLY when work done),
the spec §5.3 two-writer acceptance at full scale (2×500), and the §7
corrupt-DB fallback with a REAL garbage transcript.db.

All store access flows through the conftest autouse override
(isolate_transcript_store) except the deliberately-global tests, which
restore module globals in a finally (the fixture's monkeypatch teardown
cannot restore globals it never set — same pattern as
test_agent_persistence.py::TestSingletonPath).
"""

import json
import logging
import os
import sqlite3
import threading

import pytest

from agent import persistence
from agent.persistence import (
    conversations_dir,
    load_conversation_from_disk,
    migrate_conversations_to_store,
    run_store_migration_once,
    save_conversation_to_disk,
)
from models.conversation import Conversation, Message, MessageRole
from utils.transcript_store import TranscriptStore


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    """Patch get_config_dir for EVERY test in this module.

    NON-NEGOTIABLE (2026-10-01 incident): the first draft ran
    migrate_conversations_to_store() against the REAL config dir and renamed
    all ~4,900 live conversation files to .json.migrated (fully recovered by
    reverse rename — the sweep is non-destructive by contract, which is the
    only reason recovery was possible). conversations_dir() and the store's
    default path both derive from get_config_dir at CALL time, so patching
    here covers the sweep, the planted legacy files, and any store built
    through the real singleton path.
    """
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
    return tmp_path


def _legacy_file(sk: str, n_messages: int, roles=None) -> None:
    """Plant a legacy <sk>.json with n_messages (alternating user/assistant)."""
    msgs = []
    for i in range(n_messages):
        role = (roles[i] if roles else ("user" if i % 2 == 0 else "assistant"))
        msgs.append({"role": role, "content": f"{sk}-m{i}"})
    path = os.path.join(conversations_dir(), f"{sk}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"session_key": sk, "agent_name": "Coder", "model": "m/x",
                   "messages": msgs}, f)


def _store() -> TranscriptStore:
    return persistence._get_store()


class TestMigrateBasic:
    def test_basic_three_files(self):
        """3 legacy files (2, 5, 0 messages) → migrated=3, turns=7; store rows
        match each file's roles/contents at seq == JSON index; files renamed
        .migrated, originals gone."""
        _legacy_file("sk-a", 2)
        _legacy_file("sk-b", 5)
        _legacy_file("sk-c", 0)

        stats = migrate_conversations_to_store()

        assert stats["migrated"] == 3
        assert stats["turns"] == 7
        assert stats["errors"] == []
        assert stats["kept_on_json"] == []
        store = _store()
        for sk, n in (("sk-a", 2), ("sk-b", 5), ("sk-c", 0)):
            rows = store.load_all(sk)
            assert len(rows) == n
            for i, row in enumerate(rows):
                assert row["seq"] == i  # SP2 invariant: store seq == JSON index
                assert row["role"] == ("user" if i % 2 == 0 else "assistant")
                assert row["content"] == f"{sk}-m{i}"
        d = conversations_dir()
        for sk in ("sk-a", "sk-b", "sk-c"):
            assert not os.path.exists(os.path.join(d, f"{sk}.json"))
            assert os.path.exists(os.path.join(d, f"{sk}.json.migrated"))

    def test_progress_fires_at_most_every_10(self):
        """on_progress fires at 10/20/25 for 25 files — heartbeat, not spam."""
        for i in range(25):
            _legacy_file(f"sk-{i:02d}", 1)
        calls: list[tuple[int, int]] = []
        stats = migrate_conversations_to_store(
            on_progress=lambda done, total: calls.append((done, total))
        )
        assert stats["migrated"] == 25
        assert calls == [(10, 25), (20, 25), (25, 25)]


class TestMigrateIdempotentResumable:
    def test_second_run_finds_nothing(self):
        """Run → re-run: files are renamed away, so the sweep is a no-op.
        Store row counts unchanged; no errors."""
        _legacy_file("sk-x", 3)
        _legacy_file("sk-y", 1)
        first = migrate_conversations_to_store()
        assert first["migrated"] == 2
        counts_first = {sk: len(_store().load_all(sk)) for sk in ("sk-x", "sk-y")}

        second = migrate_conversations_to_store()

        assert second["migrated"] == 0
        assert second["errors"] == []
        counts_second = {sk: len(_store().load_all(sk)) for sk in ("sk-x", "sk-y")}
        assert counts_second == counts_first
        d = conversations_dir()
        assert [f for f in os.listdir(d) if f.endswith(".json")] == []

    def test_resumes_partial_session(self):
        """Resumable: a session whose store holds only a PREFIX of its file
        (crashed earlier launch) gets the missing tail appended — final row
        count == file message count, seqs == JSON indexes, file renamed.
        BUG#4: turns reports the 20 rows WRITTEN, not the file's 40."""
        sk = "sk-partial"
        _legacy_file(sk, 40)
        store = _store()
        # Simulate the crashed earlier launch: only the first 20 rows landed.
        store.append_delta(sk, 0, [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"{sk}-m{i}", "tool_calls": None,
             "tool_call_id": None, "tokens_used": 0}
            for i in range(20)
        ])

        stats = migrate_conversations_to_store()

        assert stats["migrated"] == 1
        rows = store.load_all(sk)
        assert len(rows) == 40  # NOT 40 + 20 — append_delta skipped the prefix
        assert [r["seq"] for r in rows] == list(range(40))
        assert [r["content"] for r in rows] == [f"{sk}-m{i}" for i in range(40)]
        # BUG#4 (SP3 fix round): turns counts ROWS WRITTEN (the resumed 20),
        # not the file size (40).
        assert stats["turns"] == 20
        d = conversations_dir()
        assert not os.path.exists(os.path.join(d, f"{sk}.json"))
        assert os.path.exists(os.path.join(d, f"{sk}.json.migrated"))


class TestMigratePartialFailure:
    def test_corrupt_file_never_blocks_sweep(self):
        """One corrupt JSON: NOT renamed, counted in errors, left as-is; the
        other 2 sessions migrate fine (per-session isolation)."""
        d = conversations_dir()
        _legacy_file("sk-ok1", 2)
        _legacy_file("sk-ok2", 1)
        corrupt = os.path.join(d, "sk-corrupt.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write("{ this is not json ]")

        stats = migrate_conversations_to_store()

        assert stats["migrated"] == 2
        assert len(stats["errors"]) == 1
        err_sk, err_repr = stats["errors"][0]
        assert err_sk == "sk-corrupt"
        assert "JSONDecodeError" in err_repr
        # Corrupt file LEFT AS-IS — retried next launch.
        assert os.path.exists(corrupt)
        assert not os.path.exists(corrupt + ".migrated")
        # The good files went through.
        for sk in ("sk-ok1", "sk-ok2"):
            assert not os.path.exists(os.path.join(d, f"{sk}.json"))
            assert os.path.exists(os.path.join(d, f"{sk}.json.migrated"))

    def test_corrupt_file_retried_next_launch(self):
        """Resumability of errors: fix the corrupt file and re-run — it
        migrates then (collected errors never blacklist a session)."""
        d = conversations_dir()
        corrupt = os.path.join(d, "sk-fix.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write("garbage")
        assert len(migrate_conversations_to_store()["errors"]) == 1

        _legacy_file("sk-fix", 2)  # overwrite with valid content
        stats = migrate_conversations_to_store()

        assert stats["migrated"] == 1
        assert stats["errors"] == []
        assert len(_store().load_all("sk-fix")) == 2


class TestMigrateSkipsCaughtUp:
    def test_dual_written_session_skipped_and_renamed(self):
        """A session already dual-written (wrapper save; store rows == file
        count) is SKIPPED, not re-appended — and the file IS renamed (it is
        fully migrated). BUG#3 teeth: MUST also assert
        len(store.load_all(sk)) >= file_message_count after migration (a
        watermark-only skip that discards rows fails this)."""
        sk = "sk-caught-up"
        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[
                Message(role=MessageRole.USER, content="u0"),
                Message(role=MessageRole.ASSISTANT, content="a0"),
                Message(role=MessageRole.USER, content="u1"),
            ],
        )
        save_conversation_to_disk(conv, sk)
        store = _store()
        assert len(store.load_all(sk)) == 3  # dual-write landed the delta

        stats = migrate_conversations_to_store()

        assert stats["skipped"] == 1
        assert stats["migrated"] == 0
        assert stats["errors"] == []
        rows = store.load_all(sk)
        file_messages = 3
        assert len(rows) >= file_messages  # the BUG#3 teeth
        assert [r["content"] for r in rows] == ["u0", "a0", "u1"]  # no re-append
        d = conversations_dir()
        assert not os.path.exists(os.path.join(d, f"{sk}.json"))
        assert os.path.exists(os.path.join(d, f"{sk}.json.migrated"))


class TestMigrateWmAheadNotRenamed:
    def test_watermark_ahead_session_not_renamed_no_loss(self):
        """BUG#1 (SP3 fix round), the Debugger's exact probe shape: 3 committed
        rows + a phantom seq-9 append puts the watermark at 9 while the file
        holds 10 messages. append_delta starts past every row → -1, ZERO rows
        written. The rename must NOT happen (it would destroy the only copy
        of 6 missing turns); the error names the session; migrated excludes
        it; a second run refuses identically (idempotent)."""
        sk = "sk-wm-ahead"
        store = _store()
        for i in range(3):
            store.append_turn(sk, "user", f"{sk}-m{i}")
        store.append_turn(sk, "user", "phantom", seq=9)  # wm → 9, 4 rows
        _legacy_file(sk, 10)
        rows_before = len(store.load_all(sk))

        first = migrate_conversations_to_store()
        second = migrate_conversations_to_store()

        for stats in (first, second):
            assert stats["migrated"] == 0
            assert stats["skipped"] == 0
            assert len(stats["errors"]) == 1
            err_sk, err_msg = stats["errors"][0]
            assert err_sk == sk
            assert "watermark ahead of rows" in err_msg
            assert "wm=9" in err_msg and "file=10" in err_msg
        # Store unchanged: the no-op append wrote nothing, twice.
        assert len(store.load_all(sk)) == rows_before == 4
        # The sole copy SURVIVES — not renamed, both runs.
        d = conversations_dir()
        assert os.path.exists(os.path.join(d, f"{sk}.json"))
        assert not os.path.exists(os.path.join(d, f"{sk}.json.migrated"))


    def test_negative_seq_keeps_file(self):
        """Round-3 BUG#1, migration level: a negative-seq row (the legacy
        DB threat — CHECK only guards fresh DBs) makes bare {0,1} look like
        count 3 = file 3 while index 2 is missing. The floor in covers()
        rejects it: NOT renamed, error recorded, store unchanged, idempotent
        second run."""
        import sqlite3 as _sq

        sk = "sk-neg-seq"
        # Legacy-schema DB (no CHECK) via the store override seam — a fresh
        # store would refuse the poison INSERT at the CHECK.
        legacy_dir = os.path.join(str(conversations_dir()), "..", "legacy-store")
        os.makedirs(legacy_dir, exist_ok=True)
        legacy_db = os.path.join(legacy_dir, "transcript.db")
        legacy = _sq.connect(legacy_db)
        legacy.execute(
            "CREATE TABLE turns ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_key TEXT NOT NULL, seq INTEGER NOT NULL,"
            " epoch INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL,"
            " content TEXT NOT NULL DEFAULT '', tool_calls TEXT,"
            " tool_call_id TEXT, tokens_used INTEGER NOT NULL DEFAULT 0,"
            " timestamp TEXT NOT NULL, UNIQUE(session_key, epoch, seq))"
        )
        legacy.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " session_key TEXT PRIMARY KEY, agent_name TEXT NOT NULL DEFAULT '',"
            " model TEXT NOT NULL DEFAULT '', provider TEXT,"
            " watermark INTEGER NOT NULL DEFAULT -1, epoch INTEGER NOT NULL DEFAULT 0,"
            " diverged INTEGER NOT NULL DEFAULT 0, updated_at TEXT)"
        )
        for seq, content in ((0, "m0"), (1, "m1"), (-1, "poison")):
            legacy.execute(
                "INSERT INTO turns (session_key, seq, epoch, role, content,"
                " tool_calls, tool_call_id, tokens_used, timestamp)"
                " VALUES (?, ?, 0, 'user', ?, NULL, NULL, 0,"
                " strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (sk, seq, content),
            )
        legacy.commit()
        legacy.close()
        override = TranscriptStore(db_path=legacy_db)
        orig = persistence._store_override
        persistence._store_override = override
        try:
            _legacy_file(sk, 3)
            rows_before = override.load_all(sk)
            first = migrate_conversations_to_store()
            second = migrate_conversations_to_store()
        finally:
            persistence._store_override = orig
            override.close()

        for stats in (first, second):
            assert stats["migrated"] == 0
            assert stats["skipped"] == 0
            assert len(stats["errors"]) == 1
            err_sk, err_msg = stats["errors"][0]
            assert err_sk == sk
            assert "count/coverage mismatch" in err_msg
        assert len(rows_before) == 3
        d = conversations_dir()
        assert os.path.exists(os.path.join(d, f"{sk}.json"))
        assert not os.path.exists(os.path.join(d, f"{sk}.json.migrated"))


class TestMigrateDivergedStaysOnJson:
    def test_diverged_session_never_renamed(self):
        """Fix-round-2 ruling: a diverged-flagged (compacted) session is NOT
        renamed, NOT appended, listed in kept_on_json, zero new store rows —
        renaming would cement a store missing turns."""
        sk = "sk-diverged"
        store = _store()
        store.append_turn(sk, "user", "old-turn-0")
        store.mark_diverged(sk)
        _legacy_file(sk, 4)
        rows_before = len(store.load_all(sk))

        stats = migrate_conversations_to_store()

        assert stats["kept_on_json"] == [sk]
        assert stats["migrated"] == 0
        assert stats["skipped"] == 0
        assert stats["errors"] == []
        assert len(store.load_all(sk)) == rows_before  # zero new rows
        d = conversations_dir()
        assert os.path.exists(os.path.join(d, f"{sk}.json"))  # still on JSON


class TestBannerGate:
    def test_banner_fires_only_when_work_done(self):
        """run_store_migration_once: 0 legacy files → NO on_complete call;
        3 files → exactly one call with the full stats dict. Latched: the
        second process-wide call returns None without re-firing."""
        completed: list[dict] = []

        # Phase 1: clean install — no card.
        stats_none = run_store_migration_once(on_complete=completed.append)
        assert stats_none is not None
        assert stats_none["migrated"] == 0
        assert completed == []  # the gate: no card when nothing moved

        # Latch fired — reset for phase 2 (this test owns the global).
        persistence._STORE_MIGRATION_DONE = False

        # Phase 2: three legacy files — exactly one card call.
        for i in range(3):
            _legacy_file(f"sk-banner-{i}", 2)
        stats = run_store_migration_once(on_complete=completed.append)
        assert stats is not None
        assert stats["migrated"] == 3
        assert len(completed) == 1
        card_stats = completed[0]
        assert card_stats["migrated"] == 3
        assert card_stats["turns"] == 6
        assert "skipped" in card_stats and "seconds" in card_stats
        assert "errors" in card_stats and "kept_on_json" in card_stats

        # Phase 3: latched — no second sweep, no second card.
        persistence._STORE_MIGRATION_DONE = False
        persistence._STORE_MIGRATION_DONE = True  # simulate already-run
        assert run_store_migration_once(on_complete=completed.append) is None
        assert len(completed) == 1

    def teardown_method(self):
        persistence._STORE_MIGRATION_DONE = False


class TestTwoWriters500Each:
    def test_same_session_500_each_zero_lost(self, tmp_path):
        """Spec §5.3 acceptance at full scale: two store instances, SAME
        session, 500+500 appends from Barrier-released threads. A fresh third
        connection must see 1000/1000 rows, contiguous seqs 0..999, and
        watermark 999."""
        db_path = str(tmp_path / "acceptance.db")
        s1 = TranscriptStore(db_path=db_path)
        s2 = TranscriptStore(db_path=db_path)
        n = 500
        errors: list[str] = []
        barrier = threading.Barrier(2)

        def writer(store: TranscriptStore) -> None:
            try:
                barrier.wait(timeout=10)
                for i in range(n):
                    store.append_turn("sk-acceptance", "user", f"turn-{i}")
            except (sqlite3.Error, RuntimeError, OSError) as exc:
                errors.append(repr(exc))

        t1 = threading.Thread(target=writer, args=(s1,))
        t2 = threading.Thread(target=writer, args=(s2,))
        t1.start()
        t2.start()
        t1.join(timeout=120)
        t2.join(timeout=120)
        assert not t1.is_alive() and not t2.is_alive()
        assert errors == []

        # Fresh third connection: what actually hit the DB file.
        fresh = TranscriptStore(db_path=db_path)
        rows = fresh.load_all("sk-acceptance")
        assert len(rows) == 2 * n
        assert sorted(r["seq"] for r in rows) == list(range(2 * n))
        assert fresh.session_watermark("sk-acceptance") == 2 * n - 1
        fresh.close()
        s1.close()
        s2.close()


class TestCorruptDbFallback:
    def test_real_corrupt_db_json_fallback(self, caplog):
        """Spec §7 edge with a REAL corrupt file: garbage transcript.db in the
        config dir → the wrapper's save still writes the JSON (and logs the
        store failure); load from JSON succeeds. Exercises the D3 fallback
        through the production singleton path (not the test seam)."""
        from utils.config import get_config_dir

        with open(os.path.join(get_config_dir(), "transcript.db"), "wb") as f:
            f.write(b"definitely not a sqlite database" * 16)

        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[Message(role=MessageRole.USER, content="survives")],
        )

        orig_override = persistence._store_override
        orig_singleton = persistence._store_singleton
        persistence._store_override = None
        persistence._store_singleton = None
        try:
            with caplog.at_level(logging.WARNING, logger="agent.persistence"):
                # Must NOT raise: JSON write succeeds, store failure is logged.
                path = save_conversation_to_disk(conv, "special:coder")
        finally:
            persistence._store_override = orig_override
            persistence._store_singleton = orig_singleton

        assert os.path.isfile(path)
        # The store failure was surfaced, not swallowed silently — the warning
        # record carries the corrupt-DB exception as exc_info (logged with
        # exc_info=True).
        assert any("transcript store append failed" in r.message for r in caplog.records)
        assert any(
            r.exc_info is not None and isinstance(r.exc_info[1], sqlite3.DatabaseError)
            and "file is not a database" in str(r.exc_info[1])
            for r in caplog.records
        )

        # JSON round-trip: the authoritative copy is intact.
        result = load_conversation_from_disk("special:coder")
        assert result is not None
        loaded, _meta = result
        assert [m.content for m in loaded.messages] == ["survives"]

    def test_corrupt_db_aborts_cleanly_with_retry(self, caplog):
        """BUG#3 (SP3 fix round): garbage transcript.db → the sweep RETURNS
        (never raises) with aborted=True and errors[0][0] == "<store>"; NO
        files renamed; the latch is UNSET (retry posture); and after the DB
        is fixed, a second sweep migrates for real. The on_complete callback
        fires with the aborted stats (the SP4 banner renders the retry
        notice from it)."""
        db_path = os.path.join(str(conversations_dir()), "..", "transcript.db")
        db_path = os.path.abspath(db_path)
        with open(db_path, "wb") as f:
            f.write(b"garbage not a database" * 16)
        _legacy_file("sk-abort-a", 2)
        _legacy_file("sk-abort-b", 3)
        completed: list[dict] = []
        # Hit the REAL singleton path: the conftest autouse override (a healthy
        # tmp store) would mask the corrupt DB entirely.
        orig_override = persistence._store_override
        orig_singleton = persistence._store_singleton
        persistence._store_override = None
        persistence._store_singleton = None
        persistence._STORE_MIGRATION_DONE = False
        try:
            with caplog.at_level(logging.WARNING, logger="agent.persistence"):
                stats = run_store_migration_once(on_complete=completed.append)
        finally:
            persistence._store_override = orig_override
            persistence._store_singleton = orig_singleton
            persistence._STORE_MIGRATION_DONE = False

        # Returned, not raised; abort shape correct.
        assert stats is not None
        assert stats["aborted"] is True
        assert stats["migrated"] == 0
        assert len(stats["errors"]) == 1
        assert stats["errors"][0][0] == "<store>"
        assert "file is not a database" in stats["errors"][0][1]
        # The card fires on abort too (the banner must say "will retry").
        assert len(completed) == 1
        assert completed[0]["aborted"] is True
        # Latch UNSET: next launch retries.
        assert persistence._STORE_MIGRATION_DONE is False
        # Nothing was renamed.
        d = conversations_dir()
        assert os.path.exists(os.path.join(d, "sk-abort-a.json"))
        assert os.path.exists(os.path.join(d, "sk-abort-b.json"))
        # The abort was logged, not silent.
        assert any("migration ABORTED" in r.getMessage() for r in caplog.records)

        # NOW fix the DB (delete the garbage file) and re-run: succeeds.
        os.remove(db_path)
        persistence._store_override = None
        persistence._store_singleton = None
        persistence._STORE_MIGRATION_DONE = False
        try:
            stats2 = run_store_migration_once(on_complete=completed.append)
        finally:
            persistence._store_override = orig_override
            persistence._store_singleton = orig_singleton
            persistence._STORE_MIGRATION_DONE = False
        assert stats2 is not None
        assert stats2["aborted"] is False
        assert stats2["migrated"] == 2
        assert not os.path.exists(os.path.join(d, "sk-abort-a.json"))


class TestLaunchWiring:
    def test_flag_default_off(self):
        """SP4A: production defaults the flag ON (main.py setdefault=1) and
        the suite pins it OFF — tests/conftest.py sets
        CRABCAKES_MIGRATE_STORE=0 at MODULE level, BEFORE any test module is
        collected (collection-time `import main` in test_cli_nudge latches
        agent.runtime's _MIGRATE_STORE_ON_INIT; a fixture-time pin would be
        too late). Both halves are pinned: the suite's latch is False here,
        and main.py still carries the setdefault (source-level pin — losing
        it would silently disable migration in production)."""
        from agent.runtime import _MIGRATE_STORE_ON_INIT
        assert _MIGRATE_STORE_ON_INIT is False

        import os
        assert os.environ["CRABCAKES_MIGRATE_STORE"] == "0"
        from pathlib import Path
        main_src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
            encoding="utf-8"
        )
        # SPEC-11 SP2 fix round (D2 + HIGH audit fix): main.py's default-ON
        # write is GATED on get_env — an OLD-name kill-switch must survive
        # it. The gated form (get_env is None → write) is what the
        # subprocess kill-switch test in tests/test_env_divergence.py pins
        # end-to-end; this source pin catches a revert to a bare setdefault,
        # which silently defeats the old name (setdefault never sees it).
        assert (
            'if get_env("MIGRATE_STORE") is None:' in main_src
            and 'os.environ["DEVELCAKES_MIGRATE_STORE"] = "1"' in main_src
        )

    def test_init_without_flag_touches_nothing(self):
        """AgentRuntime __init__ with the flag unset performs NO migration
        sweep (guard test for the opt-in gate)."""
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        persistence._STORE_MIGRATION_DONE = False
        try:
            rt = AgentRuntime(AgentConfig())
            assert rt._store_migration_thread is None
            # No sweep ran: the module latch is untouched, no thread started.
            assert persistence._STORE_MIGRATION_DONE is False
        finally:
            persistence._STORE_MIGRATION_DONE = False

    def test_init_with_flag_runs_sweep_async(self, monkeypatch):
        """With CRABCAKES_MIGRATE_STORE=1, __init__ starts the daemon sweep;
        legacy files land in the store without blocking the constructor."""
        import agent.runtime as runtime_mod
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        monkeypatch.setenv("CRABCAKES_MIGRATE_STORE", "1")
        monkeypatch.setattr(runtime_mod, "_MIGRATE_STORE_ON_INIT", True)
        orig_override = persistence._store_override
        orig_singleton = persistence._store_singleton
        persistence._store_override = None
        persistence._store_singleton = None
        persistence._STORE_MIGRATION_DONE = False
        _legacy_file("wired-sk", 2)
        try:
            rt = AgentRuntime(AgentConfig())
            thread = rt._store_migration_thread
            assert thread is not None
            assert thread.daemon is True
            thread.join(timeout=30)
            assert not thread.is_alive()
            # The sweep went through the REAL singleton path (override None)
            # on the patched config dir, and the latch fired.
            assert persistence._STORE_MIGRATION_DONE is True
            d = conversations_dir()
            assert not os.path.exists(os.path.join(d, "wired-sk.json"))
            assert os.path.exists(os.path.join(d, "wired-sk.json.migrated"))
        finally:
            # BUG#2 (SP3 fix round): BOTH globals must be restored — the
            # singleton (pointing at a deleted tmp DB) previously leaked into
            # subsequent tests, the incident class test-side.
            persistence._store_override = orig_override
            persistence._store_singleton = orig_singleton
            persistence._STORE_MIGRATION_DONE = False
        # Teardown assertion (the leak never survives this test): after the
        # finally-block restore, the singleton is the captured original —
        # NOT a TranscriptStore built on this test's deleted tmp dir.
        assert persistence._store_singleton is orig_singleton
        assert persistence._store_override is orig_override

    def test_migration_callback_dropped_after_stop(self, monkeypatch, caplog):
        """BUG#6 (SP3 fix round, guard-only): a runtime stopped before the
        sweep completes DROPS the completion callback — no raise, no
        idle_add into the torn-down loop, a log line exists. The guard
        closure is exercised exactly as production wires it: captured via
        the module seam _start_store_migration consumes, invoked on the
        daemon thread."""
        import agent.runtime as runtime_mod
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        rt = AgentRuntime(AgentConfig())
        rt.start()
        idle_calls: list = []

        class RecordingGLib:
            @staticmethod
            def idle_add(fn, *args, **kwargs):
                idle_calls.append(fn)
                return 1

        rt._GLib = RecordingGLib
        rt.stop()
        assert rt.is_running() is False

        # Capture the guarded closure through the SAME seam production uses.
        captured: dict = {}

        def fake_async(on_complete, on_progress=None):
            captured["cb"] = on_complete
            # Real thread machinery, no sweep: the closure under test is what
            # matters; the sweep itself is irrelevant to this guard test.
            t = threading.Thread(target=lambda: None, daemon=True)
            t.start()
            return t

        monkeypatch.setattr(runtime_mod, "_migrate_store_async", fake_async)
        persistence._STORE_MIGRATION_DONE = True  # belt: sweep would be a no-op
        rt._start_store_migration()
        assert rt._store_migration_thread is not None
        rt._store_migration_thread.join(timeout=10)
        guarded = captured.get("cb")
        assert callable(guarded)

        stats = {"migrated": 3, "turns": 9, "skipped": 0, "seconds": 0.1,
                 "errors": [], "kept_on_json": [], "aborted": False}
        with caplog.at_level(logging.INFO, logger="agent.runtime"):
            guarded(stats)

        assert idle_calls == []  # nothing scheduled into a stopped loop
        assert any(
            "dropping completion callback" in r.getMessage() for r in caplog.records
        )
        assert not rt.is_running()

    def test_skip_branch_count_collision_keeps_file(self):
        """BUG#1 round 2, the auditor's exact skip-branch probe: 3 rows
        (0,1,2) + phantom seq-9 → load_all count=4, file=4 — the bare COUNT
        matched, coverage does NOT (index 3 missing). NOT renamed, error
        recorded, store unchanged."""
        sk = "sk-count-collision"
        store = _store()
        for i in range(3):
            store.append_turn(sk, "user", f"{sk}-m{i}")
        store.append_turn(sk, "user", "phantom", seq=9)  # count 4, wm 9
        _legacy_file(sk, 4)
        rows_before = len(store.load_all(sk))

        stats = migrate_conversations_to_store()

        assert stats["migrated"] == 0
        assert stats["skipped"] == 0
        assert len(stats["errors"]) == 1
        err_sk, err_msg = stats["errors"][0]
        assert err_sk == sk
        assert "count/coverage mismatch" in err_msg
        assert len(store.load_all(sk)) == rows_before == 4
        d = conversations_dir()
        assert os.path.exists(os.path.join(d, f"{sk}.json"))
        assert not os.path.exists(os.path.join(d, f"{sk}.json.migrated"))

    def test_epoch_inflation_does_not_cover(self):
        """BUG#1 round 2, the auditor's multi-epoch probe: epoch-0 holds 5
        rows, bump_epoch(), epoch-1 holds 2 → load_all count=7 while the
        CURRENT epoch covers seq 0..1 only. A file of 4 must NOT rename on
        the inflated count — coverage is current-epoch-only."""
        sk = "sk-epoch-inflation"
        store = _store()
        for i in range(5):
            store.append_turn(sk, "user", f"e0-{i}")
        store.bump_epoch(sk)
        store.append_turn(sk, "user", "e1-0")
        store.append_turn(sk, "user", "e1-1")
        assert len(store.load_all(sk)) == 7  # the inflated all-epoch count
        _legacy_file(sk, 4)

        stats = migrate_conversations_to_store()

        assert stats["migrated"] == 0
        assert stats["skipped"] == 0
        assert len(stats["errors"]) == 1
        assert stats["errors"][0][0] == sk
        d = conversations_dir()
        assert os.path.exists(os.path.join(d, f"{sk}.json"))
        assert not os.path.exists(os.path.join(d, f"{sk}.json.migrated"))


class TestBannerGateErrors:
    def test_all_corrupt_fires_banner(self):
        """BUG#3 (round 2): an all-corrupt sweep (migrated=0, aborted=False,
        errors=3) must fire the callback — the banner's "N sessions failed,
        will retry next launch" line depends on errors reaching it."""
        d = conversations_dir()
        for name in ("sk-bad-1", "sk-bad-2", "sk-bad-3"):
            with open(os.path.join(d, f"{name}.json"), "w", encoding="utf-8") as f:
                f.write("{ not json ]")
        completed: list[dict] = []
        persistence._STORE_MIGRATION_DONE = False
        try:
            stats = run_store_migration_once(on_complete=completed.append)
        finally:
            persistence._STORE_MIGRATION_DONE = False

        assert stats is not None
        assert stats["migrated"] == 0
        assert stats["aborted"] is False
        assert len(stats["errors"]) == 3
        # The gate fired exactly once with the errors attached.
        assert len(completed) == 1
        assert completed[0] is stats


class TestInitOrderGuard:
    def test_sweep_completing_during_init_dispatches(self, monkeypatch):
        """BUG#2 (round 2): a synchronous on_complete DURING __init__ (the
        daemon thread wins the race) must DISPATCH — _running is False at
        that moment but _stopped is False too; the guard reads _stopped
        only. No AttributeError, no false drop, idle_add called."""
        import agent.runtime as runtime_mod
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        idle_calls: list = []
        banner_calls: list = []

        class RecordingGLib:
            @staticmethod
            def idle_add(fn, *args, **kwargs):
                idle_calls.append(fn)
                return 1

        def recording_banner(stats):
            banner_calls.append(stats)

        monkeypatch.setattr(runtime_mod, "_log_store_migration_banner", recording_banner)

        def sync_async(on_complete, on_progress=None):
            # Invoke completion SYNCHRONOUSLY — mid-__init__, before the
            # constructor proceeds; self._GLib is still None at this moment,
            # so _dispatch runs the receiver inline (no main loop exists).
            if on_complete is not None:
                on_complete({"migrated": 1, "turns": 2, "skipped": 0,
                             "seconds": 0.0, "errors": [], "kept_on_json": [],
                             "aborted": False})
            return threading.Thread(target=lambda: None, daemon=True)

        monkeypatch.setattr(runtime_mod, "_migrate_store_async", sync_async)
        monkeypatch.setattr(runtime_mod, "_MIGRATE_STORE_ON_INIT", True)
        persistence._STORE_MIGRATION_DONE = False
        try:
            rt = AgentRuntime(AgentConfig())
            rt._GLib = RecordingGLib
        finally:
            persistence._STORE_MIGRATION_DONE = False

        # The closure dispatched during __init__ (banner receiver invoked,
        # no AttributeError swallowed, no drop logged).
        assert len(banner_calls) == 1
        assert banner_calls[0]["migrated"] == 1
        assert rt._running is False   # not started yet
        assert rt._stopped is False   # but NOT stopped — dispatch was correct



class TestRestartClearsStopped:
    def test_restart_clears_stopped(self, monkeypatch, caplog):
        """Round-3 BUG#3: start() must CLEAR _stopped — stop() latches the
        dispatch window shut; a restart reopens it. start→stop→start→
        completion → dispatched (not dropped), _stopped False."""
        import agent.runtime as runtime_mod
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        rt = AgentRuntime(AgentConfig())
        rt.start()
        rt.stop()
        assert rt._stopped is True  # the latch closed
        rt.start()
        assert rt._stopped is False  # the window reopened

        # Completion after restart: dispatched, not dropped.
        dispatched: list = []
        monkeypatch.setattr(
            runtime_mod, "_log_store_migration_banner", lambda s: dispatched.append(s)
        )
        stats = {"migrated": 1, "turns": 2, "skipped": 0, "seconds": 0.0,
                 "errors": [], "kept_on_json": [], "aborted": False}
        # Invoke the guarded closure via the same seam as the stop-drop test.
        captured: dict = {}

        def fake_async(on_complete, on_progress=None):
            captured["cb"] = on_complete
            t = threading.Thread(target=lambda: None, daemon=True)
            t.start()
            return t

        monkeypatch.setattr(runtime_mod, "_migrate_store_async", fake_async)
        rt._start_store_migration()
        thread = rt._store_migration_thread
        assert thread is not None
        thread.join(timeout=10)
        captured["cb"](stats)

        assert len(dispatched) == 1  # NOT dropped
        assert rt._stopped is False


class TestDuringInitIdleAddPin:
    def test_during_init_completion_takes_idle_add_path(self, monkeypatch):
        """Round-3 BUG#4: pins the WIRED dispatch path. The round-2
        during-init test ran the receiver inline (GLib None mid-init); this
        one constructs the runtime WITH GLib= injected so _dispatch takes
        the real idle_add path during __init__ → RecordingGLib saw the call."""
        import agent.runtime as runtime_mod
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        idle_calls: list = []

        class RecordingGLib:
            @staticmethod
            def idle_add(fn, *args, **kwargs):
                idle_calls.append(fn)
                return 1

        monkeypatch.setattr(
            runtime_mod, "_log_store_migration_banner", lambda s: None
        )

        def sync_async(on_complete, on_progress=None):
            if on_complete is not None:
                on_complete({"migrated": 1, "turns": 2, "skipped": 0,
                             "seconds": 0.0, "errors": [], "kept_on_json": [],
                             "aborted": False})
            return threading.Thread(target=lambda: None, daemon=True)

        monkeypatch.setattr(runtime_mod, "_migrate_store_async", sync_async)
        monkeypatch.setattr(runtime_mod, "_MIGRATE_STORE_ON_INIT", True)
        persistence._STORE_MIGRATION_DONE = False
        try:
            rt = AgentRuntime(AgentConfig(), GLib=RecordingGLib)
        finally:
            persistence._STORE_MIGRATION_DONE = False

        assert len(idle_calls) >= 1  # the real idle_add path fired
        assert rt._stopped is False


class TestBannerCardReceiver:
    """SP4A Edit 2 — the handler-owned FeedCardData banner (Ruling 2) +
    on_progress consumption, asserted at the ARH receiver."""

    @staticmethod
    def _stats(**overrides) -> dict:
        base = {
            "migrated": 0, "turns": 0, "skipped": 0, "seconds": 0.0,
            "errors": [], "kept_on_json": [], "aborted": False,
        }
        base.update(overrides)
        return base

    def _receiver(self):
        from unittest.mock import MagicMock

        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        return AgentRuntimeHandler(MagicMock(), MagicMock(), GLib_module=None)

    def _build_card(self, handler, stats) -> list:
        """Drive the receiver's card-construction path with a stub feed
        handler capturing add_card (headless: no GLib → inline dispatch)."""
        cards: list = []
        handler._fh = type(
            "FH", (), {"add_card": staticmethod(lambda c: cards.append(c) or len(cards))}
        )()
        handler._on_store_migration_complete(stats)
        return cards

    def test_banner_card_built_from_stats(self):
        """Three shapes → three titles: success (migrated>0), aborted
        (store unavailable), all-errors (migrated 0 + errors) — bodies carry
        migrated/turns/skipped/seconds/kept-on-JSON/errors counts."""
        handler = self._receiver()

        ok_cards = self._build_card(handler, self._stats(
            migrated=2, turns=7, skipped=1, seconds=0.4,
            kept_on_json=["special:diverged"],
        ))
        assert len(ok_cards) == 1
        assert ok_cards[0].title == "Transcript migration complete"
        assert ok_cards[0].project_name == "(none)"  # no active project
        assert ok_cards[0].metadata["kind"] == "store_migration"
        for token in ("2 session(s)", "7 turn(s)", "0.4s",
                      "special:diverged", "Errors: 0"):
            assert token in ok_cards[0].body

        abort_cards = self._build_card(handler, self._stats(
            aborted=True, errors=[("<store>", "DatabaseError(...)")]
        ))
        assert abort_cards[0].title == "Transcript migration failed"
        assert "retry on next launch" in abort_cards[0].body

        err_cards = self._build_card(handler, self._stats(
            seconds=0.2, errors=[("sk-a", "e1"), ("sk-b", "e2")]
        ))
        assert err_cards[0].title == "Transcript migration: 2 sessions need retry"
        assert "sk-a" in err_cards[0].body and "sk-b" in err_cards[0].body

    def test_banner_emits_through_feed_seam(self):
        """The card flows through self._fh.add_card (the SPEC-02 seam) as a
        system card; no hidden receiver-side dedupe — suppression belongs to
        the sweep's process latch (an aborted→retry second card is
        informative, so the receiver never silently drops it)."""
        handler = self._receiver()
        cards = self._build_card(handler, self._stats(migrated=1))
        assert len(cards) == 1 and cards[0].card_type == "system"
        cards2 = self._build_card(handler, self._stats(aborted=True))
        assert len(cards2) == 1  # retry-after-abort still renders

    def test_registration_at_runtime_construction(self, monkeypatch):
        """Ruling 2 wiring pin: _get_runtime registers the handler-owned
        receivers on every AgentRuntime it constructs — the banner card is
        handler-built, not runtime-built."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import agent.config as agent_config_mod
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        handler = AgentRuntimeHandler(MagicMock(), MagicMock(), GLib_module=None)
        fake_config = SimpleNamespace(
            default_provider="testprov",
            providers={"testprov": SimpleNamespace(
                name="testprov", api_key="sk-test", default_model="m/x",
            )},
        )
        monkeypatch.setattr(agent_config_mod, "load_agent_config",
                            lambda: fake_config)
        agent_def = SimpleNamespace(llm_name="testprov", tools=None,
                                    mcp_servers=None, role="tester",
                                    api_key=None, app_title="",
                                    fallback_provider=None)
        try:
            rt = handler._get_runtime("Tester", agent_def=agent_def)
            # Bound methods are recreated on every attribute access —
            # compare __func__ (the registration holds one bound instance).
            assert rt._on_store_migration_callback.__func__ is (
                handler._on_store_migration_complete.__func__
            )
            assert rt._on_store_migration_callback.__self__ is handler
            assert rt._on_store_migration_progress.__func__ is (
                handler._on_store_migration_progress.__func__
            )
        finally:
            for rt in handler._runtimes.values():
                rt.stop()

    def test_production_order_banner_fires(self, monkeypatch):
        """BUG#1 (SP4A fix round): the construct→wire→fire order production
        actually runs — __init__ starts the sweep BEFORE the handler wires
        set_on_store_migration, so the completion dispatch must read the
        receiver AT DISPATCH TIME. A launch-time closure snapshot captures
        the pre-wire None and the banner silently falls to the log fallback
        (the suite stayed green because no test exercised this order — this
        test is that gap, patched to fail if the fallback wins)."""
        import agent.runtime as runtime_mod
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        # The fallback is patched to RAISE: if the dispatch ever lands on
        # the log fallback, this test fails loudly instead of silently
        # passing on a fallback-only path.
        def _fallback_must_not_fire(stats: dict) -> None:
            raise AssertionError(
                "log fallback fired — the handler-wired receiver was dropped"
            )

        monkeypatch.setattr(
            runtime_mod, "_log_store_migration_banner", _fallback_must_not_fire
        )

        captured: dict = {}

        def fake_async(on_complete, on_progress=None):
            captured["cb"] = on_complete
            t = threading.Thread(target=lambda: None, daemon=True)
            t.start()
            return t

        monkeypatch.setattr(runtime_mod, "_migrate_store_async", fake_async)
        monkeypatch.setattr(runtime_mod, "_MIGRATE_STORE_ON_INIT", True)
        persistence._STORE_MIGRATION_DONE = False
        try:
            rt = AgentRuntime(AgentConfig())  # __init__ starts the sweep
            # ... and the handler wires AFTER construction (production order).
            received: list[dict] = []
            rt.set_on_store_migration(on_complete=received.append)
            assert rt._store_migration_thread is not None
            rt._store_migration_thread.join(timeout=10)

            stats = {"migrated": 1, "turns": 2, "skipped": 0, "seconds": 0.0,
                     "errors": [], "kept_on_json": [], "aborted": False}
            captured["cb"](stats)  # the sweep's completion, post-wire
        finally:
            persistence._STORE_MIGRATION_DONE = False

        assert received == [stats], (
            "receiver never fired — dispatch used the launch-time snapshot"
        )
        assert rt._stopped is False

    def test_progress_consumed(self, monkeypatch):
        """on_progress wired through the runtime setter reaches the ARH
        receiver at the SP3 heartbeat cadence (10/20/total), via idle_add;
        post-stop heartbeats are dropped (BUG#6's window)."""
        import agent.runtime as runtime_mod
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime

        rt = AgentRuntime(AgentConfig())
        received: list[tuple[int, int]] = []
        rt.set_on_store_migration(
            on_complete=lambda stats: None,
            on_progress=lambda done, total: received.append((done, total)),
        )
        assert rt._on_store_migration_progress is not None

        # Drive the guarded progress closure via the production seam.
        idle_calls: list = []

        class RecordingGLib:
            @staticmethod
            def idle_add(fn, *args, **kwargs):
                idle_calls.append(fn)
                return 1

        rt._GLib = RecordingGLib
        captured: dict = {}

        def fake_async(on_complete, on_progress=None):
            captured["progress"] = on_progress
            t = threading.Thread(target=lambda: None, daemon=True)
            t.start()
            return t

        monkeypatch.setattr(runtime_mod, "_migrate_store_async", fake_async)
        persistence._STORE_MIGRATION_DONE = True  # belt: no real sweep
        try:
            rt._start_store_migration()
            thread = rt._store_migration_thread
            assert thread is not None
            thread.join(timeout=10)
            captured["progress"](10, 25)
            captured["progress"](20, 25)
            captured["progress"](25, 25)
        finally:
            persistence._STORE_MIGRATION_DONE = False

        # Each heartbeat scheduled the receiver through idle_add (the GLib
        # main-loop hop), not dropped.
        assert len(idle_calls) == 3
        for fn in idle_calls:
            fn()  # run the dispatched main-loop body
        assert received == [(10, 25), (20, 25), (25, 25)]

        # Post-stop the same heartbeats are DROPPED (BUG#6's window).
        rt.stop()
        idle_calls.clear()
        captured["progress"](5, 25)
        assert idle_calls == [] and received == [(10, 25), (20, 25), (25, 25)]
