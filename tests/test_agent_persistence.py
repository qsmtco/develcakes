"""Tests for agent/persistence.py — conversation disk I/O.

Extracted from agent/runtime.py Phase 6. Tests exercise persistence
WITHOUT instantiating AgentRuntime — pure module-level function tests.
"""

import json
import os
import threading

import pytest

import agent.persistence as persistence  # noqa: PLR0402 — matches HEAD style
from agent.persistence import (
    conversations_dir,
    load_conversation_from_disk,
    migrate_conversation_files,
    resolve_api_key_for_conversation,
    resolve_session_workspace,
    save_conversation_to_disk,
)
from models.conversation import Conversation, Message, MessageRole, ToolCall
from utils.transcript_store import TranscriptStore


# ── SP2: dual-write + watermark (SPEC-08 D3/D4) ──────────────────────────────

# Fix-round BUG#1 constants: the production secret (must be ABSENT) and the
# planted tool_calls-arguments leak (must be FOUND by the sweep — teeth).
SECRET = "sk-SECRET-TEST-12345"
LEAK_IN_ARGS = "sk-LEAKED-IN-ARGS"


def _store_turns(store: TranscriptStore, session_key: str) -> list[dict]:
    return store.load_all(session_key)


class TestDualWriteStore:
    """save_conversation_to_disk dual-writes: JSON first (authoritative), then
    an index/watermark-based delta append to the transcript store."""

    def test_save_appends_delta_to_store(self, tmp_path, monkeypatch):
        """3-message conv -> 3 turns at seqs 0/1/2 (watermark 2); saving with
        one more message appends seq 3 (store shows 4, NOT 7)."""
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[
                Message(role=MessageRole.USER, content="m0"),
                Message(role=MessageRole.ASSISTANT, content="m1"),
                Message(role=MessageRole.TOOL_RESULT, content="m2", tool_call_id="c1"),
            ],
        )
        save_conversation_to_disk(conv, "special:coder")
        turns = _store_turns(store, "special:coder")
        assert [t["seq"] for t in turns] == [0, 1, 2]
        assert [t["role"] for t in turns] == ["user", "assistant", "tool"]
        assert store.session_watermark("special:coder") == 2

        conv.add_user_message("m3")
        save_conversation_to_disk(conv, "special:coder")
        turns = _store_turns(store, "special:coder")
        assert len(turns) == 4
        assert [t["seq"] for t in turns] == [0, 1, 2, 3]
        assert turns[3]["content"] == "m3"

    def test_save_twice_same_conv_no_duplicates(self, tmp_path, monkeypatch):
        """Watermark gate holds: an unchanged re-save appends NOTHING (3 stays
        3 — the pre-SP2 behavior would have been a whole-file rewrite)."""
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[Message(role=MessageRole.USER, content=f"m{i}") for i in range(3)],
        )
        save_conversation_to_disk(conv, "special:coder")
        save_conversation_to_disk(conv, "special:coder")
        turns = _store_turns(store, "special:coder")
        assert len(turns) == 3
        assert [t["seq"] for t in turns] == [0, 1, 2]

    def test_store_failure_falls_back_to_json(self, tmp_path, monkeypatch):
        """A raising store must not break the save: JSON lands, no exception
        escapes (D3 fallback contract — JSON is authoritative this release)."""
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))

        class RaisingStore:
            def session_watermark(self, session_key: str) -> int:
                raise RuntimeError("store exploded")

            def append_delta(self, session_key: str, base_idx: int, rows: list) -> int:
                raise RuntimeError("store exploded")

        monkeypatch.setattr(persistence, "_store_override", RaisingStore())
        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[Message(role=MessageRole.USER, content="hello")],
        )
        path = save_conversation_to_disk(conv, "special:coder")  # must not raise
        assert os.path.isfile(path)
        with open(path) as f:
            assert json.load(f)["messages"][0]["content"] == "hello"

    def test_concurrent_saves_union_preserved(self, tmp_path, monkeypatch):
        """BUG#4 (SP2 fix round): two racing saves of different convs under ONE
        session key must BOTH survive — the atomic append_delta serializes them
        (one lock + one BEGIN IMMEDIATE, wm re-read INSIDE the tx), so the
        second writer re-derives from the first's COMMITTED watermark and the
        union lands. The old per-row loop raced between its wm read and its
        inserts, silently dropping rows.
        """
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        # Shared 5-message prefix, then 3 divergent tail messages (B is longer).
        def _conv(n: int, tail_role) -> Conversation:
            msgs = [Message(role=MessageRole.USER, content=f"shared-{i}") for i in range(5)]
            msgs += [
                Message(role=tail_role, content=f"tail-{i}") for i in range(n - 5)
            ]
            return Conversation(
                agent_name="Coder", model="openai/gpt-4o", messages=msgs
            )

        conv_a = _conv(5, MessageRole.USER)  # 5 messages: shared prefix only
        conv_b = _conv(8, MessageRole.ASSISTANT)  # 8: shared prefix + 3 tail
        barrier = threading.Barrier(2)
        errors: list[str] = []

        def saver(conv):
            try:
                barrier.wait()
                save_conversation_to_disk(conv, "sk-shared")
            except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
                errors.append(repr(exc))

        t1 = threading.Thread(target=saver, args=(conv_a,))
        t2 = threading.Thread(target=saver, args=(conv_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == [], f"escaped exceptions: {errors}"

        turns = _store_turns(store, "sk-shared")
        assert len(turns) == 8  # the UNION — not 5, not a partial overwrite
        assert [t["seq"] for t in turns] == list(range(8))
        assert store.session_watermark("sk-shared") == 7
        # Shared prefix: either writer's rows (identical content), then B's tail.
        assert [t["content"] for t in turns[:5]] == [f"shared-{i}" for i in range(5)]
        assert [t["content"] for t in turns[5:]] == ["tail-0", "tail-1", "tail-2"]

        # ── Round-2 fold — divergent-content teeth: same indexes, DIFFERENT
        # content. The loser's rows must be SKIPPED (first-committer-wins),
        # never merged into a mixed tail.
        def _divergent_conv(tail_label: str) -> Conversation:
            msgs = [
                Message(role=MessageRole.USER, content=f"shared-{i}") for i in range(5)
            ]
            msgs += [
                Message(role=MessageRole.USER, content=f"{tail_label}-tail-{i}")
                for i in range(3)
            ]
            return Conversation(agent_name="Coder", model="openai/gpt-4o", messages=msgs)

        conv_ad = _divergent_conv("A")
        conv_bd = _divergent_conv("B")
        barrier2 = threading.Barrier(2)
        errors2: list[str] = []

        def saver2(conv):
            try:
                barrier2.wait()
                save_conversation_to_disk(conv, "sk-divergent")
            except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
                errors2.append(repr(exc))

        t3 = threading.Thread(target=saver2, args=(conv_ad,))
        t4 = threading.Thread(target=saver2, args=(conv_bd,))
        t3.start()
        t4.start()
        t3.join()
        t4.join()
        assert errors2 == [], f"escaped exceptions: {errors2}"

        turns_d = _store_turns(store, "sk-divergent")
        assert [t["content"] for t in turns_d[:5]] == [f"shared-{i}" for i in range(5)]
        tails = [t["content"] for t in turns_d[5:]]
        assert len(tails) == 3
        assert all(c.startswith("A-tail-") for c in tails) or all(
            c.startswith("B-tail-") for c in tails
        ), f"mixed tail contents — losers merged, not skipped: {tails}"
        assert store.session_watermark("sk-divergent") == 7

    def test_front_trim_suspends_delta_and_flags(
        self, tmp_path, monkeypatch, caplog
    ):
        """BUG#2 (round-2): context compaction FRONT-TRIMS conv.messages —
        every index shifts, so the index-based delta would mis-attribute and
        silently drop turns (the audit repro: new0/new1 never written, 4 rows
        mis-attributed). Ruling: post-trim divergence is legitimate, NEVER
        rebuilt — the session goes diverged-flagged + JSON-only, the store's
        rows stay as the audit ledger, and the warning fires ONCE (transition
        only)."""
        import logging as _logging

        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[Message(role=MessageRole.USER, content=f"m{i}") for i in range(10)],
        )
        save_conversation_to_disk(conv, "special:coder")
        assert len(_store_turns(store, "special:coder")) == 10

        # Simulate compaction: drop the 2 OLDEST messages, then 5 NEW turns.
        del conv.messages[0:2]
        conv.add_user_message("new0")
        conv.add_user_message("assistant0")
        conv.add_user_message("new1")
        conv.add_user_message("assistant1")
        conv.add_user_message("new2")
        assert len(conv.messages) == 13

        with caplog.at_level(_logging.WARNING, logger="agent.persistence"):
            save_conversation_to_disk(conv, "special:coder")
            # JSON has all 13 (authoritative working file).
            with open(os.path.join(conversations_dir(), "special:coder.json")) as f:
                assert len(json.load(f)["messages"]) == 13
            # Store: STILL the original 10 rows, original contents — no
            # mis-attribution, no new rows.
            turns = _store_turns(store, "special:coder")
            assert len(turns) == 10
            assert [t["content"] for t in turns] == [f"m{i}" for i in range(10)]
            assert store.session_watermark("special:coder") == 9
            assert store.is_diverged("special:coder") is True

        # Second save (+1 more msg): still no store append, and NO second
        # warning (flag already set — transition-only).
        with caplog.at_level(_logging.WARNING, logger="agent.persistence"):
            warnings_before = len(caplog.records)
            conv.add_user_message("new3")
            save_conversation_to_disk(conv, "special:coder")
            assert len(_store_turns(store, "special:coder")) == 10
            assert len(caplog.records) == warnings_before

        # ── Round-3 BUG#1 KILL TEST — the MIDDLE trim (keep_first=2 shape).
        # del messages[2:4] preserves index 0 AND the length floor (13 >= 10),
        # so the round-2 seq-0-only guard sailed through: delta appended from
        # base_idx 10 and mis-attributed the shifted tail. The boundary anchor
        # (row_at(seq=wm) vs messages[wm]) must catch it: any trim in [0..wm]
        # shifts messages[wm]; appends above wm never do.
        fresh_conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[Message(role=MessageRole.USER, content=f"k{i}") for i in range(10)],
        )
        save_conversation_to_disk(fresh_conv, "sk-middle")
        snapshot = [t["content"] for t in _store_turns(store, "sk-middle")]
        assert snapshot == [f"k{i}" for i in range(10)]
        assert store.session_watermark("sk-middle") == 9

        del fresh_conv.messages[2:4]  # keeps 0 and 1 — the keep_first=2 shape
        fresh_conv.add_user_message("mid-new0")
        fresh_conv.add_assistant_message("mid-new1")
        fresh_conv.add_user_message("mid-new2")
        fresh_conv.add_assistant_message("mid-new3")
        fresh_conv.add_user_message("mid-new4")
        assert len(fresh_conv.messages) == 13

        with caplog.at_level(_logging.WARNING, logger="agent.persistence"):
            save_conversation_to_disk(fresh_conv, "sk-middle")
            assert store.is_diverged("sk-middle") is True
            # Zero mis-attribution: store keeps the ORIGINAL 10 rows, byte-identical.
            turns_mid = _store_turns(store, "sk-middle")
            assert [t["content"] for t in turns_mid] == snapshot
            assert store.session_watermark("sk-middle") == 9
            # JSON has all 13 (authoritative working file).
            with open(os.path.join(conversations_dir(), "sk-middle.json")) as f:
                assert len(json.load(f)["messages"]) == 13

    def test_full_clear_flags_diverged(self, tmp_path, monkeypatch, caplog):
        """BUG#2 (round 3): a FULL message-clear hits the guard with an empty
        list. The old guard indexed messages[0] BEFORE the emptiness clause —
        IndexError, swallowed by the save fallback, never flagged (the
        emptiness check was dead code). Now emptiness is handled FIRST with
        the same flag+return path: JSON stays authoritative (emptied), the
        store row is retained as the audit ledger, one warning on transition."""
        import logging as _logging

        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[Message(role=MessageRole.USER, content="only")],
        )
        save_conversation_to_disk(conv, "special:coder")
        assert store.session_watermark("special:coder") == 0

        conv.messages.clear()
        with caplog.at_level(_logging.WARNING, logger="agent.persistence"):
            path = save_conversation_to_disk(conv, "special:coder")  # must not raise
            assert os.path.isfile(path)  # JSON still written (authoritative, emptied)
            assert store.is_diverged("special:coder") is True
            assert len(caplog.records) == 1  # exactly one warning on the transition
        # Store row retained — never rebuilt.
        turns = _store_turns(store, "special:coder")
        assert len(turns) == 1
        assert turns[0]["content"] == "only"
        # Idempotent: a second save adds NO second warning (transition-only).
        with caplog.at_level(_logging.WARNING, logger="agent.persistence"):
            before = len(caplog.records)
            save_conversation_to_disk(conv, "special:coder")
            assert len(caplog.records) == before

    def test_real_strategy_compaction_flags(self, tmp_path, monkeypatch):
        """BUG#1/#4 (round 3): production reachability pin. The REAL
        DefaultContextStrategy().compact() (keep_first=2) preserves index 0,
        which is exactly the shape the round-2 seq-0-only anchor could not
        catch. Driven over a saved conversation with post-compact appends:
        the guard must flag diverged and the store must keep the pre-compact
        snapshot byte-identical (zero mis-attributed rows)."""
        from agent.context_strategy import DefaultContextStrategy

        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        for i in range(12):
            conv.add_user_message(f"filler-{i}-" + "x" * 200)
            conv.add_assistant_message(f"reply-{i}-" + "y" * 200)
        save_conversation_to_disk(conv, "special:coder")
        pre_compact = [t["content"] for t in _store_turns(store, "special:coder")]
        wm_before = store.session_watermark("special:coder")
        assert wm_before == len(conv.messages) - 1

        DefaultContextStrategy().compact(conv, token_budget=100)
        # Sanity: the REAL strategy actually trimmed (below the length floor).
        assert len(conv.messages) < wm_before + 1

        conv.add_user_message("post-compact-0")
        conv.add_assistant_message("post-compact-1")
        save_conversation_to_disk(conv, "special:coder")

        assert store.is_diverged("special:coder") is True
        # Zero mis-attribution: store == the pre-compact snapshot exactly.
        turns = _store_turns(store, "special:coder")
        assert [t["content"] for t in turns] == pre_compact

    def test_append_only_history_still_dual_writes(self, tmp_path, monkeypatch):
        """The guard must NOT false-fire on normal append-only history: three
        growing saves all dual-write; every row lands. Round-3 extension: a
        MULTI-message grow past an established boundary (len jumps from wm+1
        to wm+4) must not trip the seq-wm anchor — appends above wm never
        shift messages[wm]."""
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o", messages=[])
        for i in range(3):
            conv.add_user_message(f"grow-{i}")
            save_conversation_to_disk(conv, "special:coder")
        turns = _store_turns(store, "special:coder")
        assert [t["content"] for t in turns] == ["grow-0", "grow-1", "grow-2"]
        assert store.session_watermark("special:coder") == 2
        assert store.is_diverged("special:coder") is False

        # Round-3: boundary established at wm=2 — now grow by THREE in one
        # save. messages[2] is unchanged ("grow-2") and messages[0] is
        # unchanged; the dual anchor must pass and all three rows must land.
        conv.add_user_message("grow-3")
        conv.add_assistant_message("grow-4")
        conv.add_user_message("grow-5")
        save_conversation_to_disk(conv, "special:coder")
        turns = _store_turns(store, "special:coder")
        assert [t["content"] for t in turns] == [
            "grow-0", "grow-1", "grow-2", "grow-3", "grow-4", "grow-5",
        ]
        assert [t["seq"] for t in turns] == [0, 1, 2, 3, 4, 5]
        assert store.session_watermark("special:coder") == 5
        assert store.is_diverged("special:coder") is False

    def test_diverged_session_survives_store_restart(self, tmp_path, monkeypatch):
        """The diverged flag persists in the DB (not in-memory): a NEW store
        instance on the same file still reports it."""
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        db_path = str(tmp_path / "t.db")
        store = TranscriptStore(db_path=db_path)
        monkeypatch.setattr(persistence, "_store_override", store)
        store.mark_diverged("special:coder")

        reopened = TranscriptStore(db_path=db_path)
        assert reopened.is_diverged("special:coder") is True
        reopened.close()

    def test_json_only_restart_backfills_on_next_save(self, tmp_path, monkeypatch):
        """JSON-only restart, per the watermark ruling (BUG#2/#3): the store's
        wm is 'appended through', NEVER 'acknowledged through' — so a restart
        that lost the store needs NO sync. Simulated here by DELETING the store
        DB (restart-with-lost-store). Load is pure JSON (3 msgs). The next save
        of the UNCHANGED conv finds wm=-1 and append_delta backfills ALL 3 JSON
        messages — that IS D3's gradual self-migration (SP3 converges to the
        same rows). Asserted: 3 rows at seqs 0..2, wm == 2 — NOT 0 rows / wm -1
        (delta skipped) and never a wm=2-with-0-rows gap (the old sync's
        watermark-without-rows lie).
        """
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        db_path = str(tmp_path / "t.db")
        store = TranscriptStore(db_path=db_path)
        monkeypatch.setattr(persistence, "_store_override", store)

        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            messages=[Message(role=MessageRole.USER, content=f"m{i}") for i in range(3)],
        )
        save_conversation_to_disk(conv, "special:coder")
        assert len(_store_turns(store, "special:coder")) == 3

        # "Restart with lost store": close + DELETE the DB file entirely (the
        # SP3-upgrade-over-legacy-JSON shape — empty store, full JSON).
        store.close()
        os.remove(db_path)
        fresh_store = TranscriptStore(db_path=db_path)
        monkeypatch.setattr(persistence, "_store_override", fresh_store)
        assert fresh_store.session_watermark("special:coder") == -1

        # Load: pure JSON — no store interaction, no watermark sync.
        result = load_conversation_from_disk("special:coder")
        assert result is not None
        loaded_conv, _data = result
        assert len(loaded_conv.messages) == 3
        assert fresh_store.session_watermark("special:coder") == -1  # untouched
        assert _store_turns(fresh_store, "special:coder") == []  # load appends nothing

        # The UNCHANGED next save backfills ALL of history through the delta.
        save_conversation_to_disk(loaded_conv, "special:coder")
        turns = _store_turns(fresh_store, "special:coder")
        assert len(turns) == 3
        assert [t["seq"] for t in turns] == [0, 1, 2]
        assert [t["content"] for t in turns] == ["m0", "m1", "m2"]
        assert fresh_store.session_watermark("special:coder") == 2
        fresh_store.close()

    def test_no_api_key_in_store(self, tmp_path, monkeypatch):
        """HIGH-3 store-surface proof (BUG#1, SP2 fix round): a conversation
        carrying an api_key saves with ZERO trace of it in the store — swept
        across ALL THREE files (db + -wal + -shm), which requires a
        wal_checkpoint(TRUNCATE) first (an active WAL re-holds committed pages
        in the sidecar). Includes a teeth-proof control: the same sweep run
        against a deliberately-leaky control MUST find the planted secret —
        the sweep itself can fail, or the proof is worthless.
        """
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        store = TranscriptStore(db_path=str(tmp_path / "t.db"))
        monkeypatch.setattr(persistence, "_store_override", store)

        def sweep(store_obj) -> set[str]:
            """Truncate the WAL into the main db, then sweep every file."""
            store_obj._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            found: set[str] = set()
            for suffix in ("", "-wal", "-shm"):
                path = store_obj._db_path + suffix
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        raw = f.read()
                    for secret in (SECRET, LEAK_IN_ARGS):
                        if secret.encode() in raw:
                            found.add(secret)
            return found

        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            provider="openai",
            api_key="sk-SECRET-TEST-12345",
            messages=[
                Message(role=MessageRole.USER, content="hello"),
                Message(
                    role=MessageRole.ASSISTANT,
                    content="calling tool",
                    tool_calls=[ToolCall(call_id="c1", tool_name="read_file", arguments={})],
                ),
            ],
        )
        save_conversation_to_disk(conv, "special:coder")
        assert sweep(store) == set()  # production path: clean

        # ── teeth control: plant a secret-shaped value in tool_calls args and
        # prove the SAME sweep finds it in a deliberately-leaky db ──
        leaky = TranscriptStore(db_path=str(tmp_path / "leaky.db"))
        leaky_store_override = persistence._store_override
        try:
            monkeypatch.setattr(persistence, "_store_override", leaky)
            leaky_conv = Conversation(
                agent_name="Coder",
                model="openai/gpt-4o",
                messages=[
                    Message(
                        role=MessageRole.ASSISTANT,
                        content="calling tool",
                        tool_calls=[
                            ToolCall(
                                call_id="c1",
                                tool_name="read_file",
                                arguments={"env": {"OPENAI_API_KEY": LEAK_IN_ARGS}},
                            )
                        ],
                    )
                ],
            )
            save_conversation_to_disk(leaky_conv, "special:leaky")
            assert sweep(leaky) == {LEAK_IN_ARGS}  # the sweep has teeth
        finally:
            monkeypatch.setattr(persistence, "_store_override", leaky_store_override)
            leaky.close()


class TestConversationsDir:
    """conversations_dir() creates and returns the conversations directory."""

    def test_creates_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        d = conversations_dir()
        assert os.path.isdir(d)


class TestSaveLoadRoundtrip:
    """save_conversation_to_disk + load_conversation_from_disk roundtrip."""

    def test_save_and_load_preserves_messages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            system_prompt="You are Coder",
            messages=[Message(role=MessageRole.USER, content="hello")],
        )
        path = save_conversation_to_disk(conv, "special:coder")
        assert os.path.isfile(path)
        result = load_conversation_from_disk("special:coder")
        assert result is not None
        loaded_conv, _data = result
        assert loaded_conv.agent_name == "Coder"
        assert len(loaded_conv.messages) == 1
        assert loaded_conv.messages[0].content == "hello"

    def test_saved_file_does_not_contain_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        conv = Conversation(
            agent_name="Coder",
            model="openai/gpt-4o",
            provider="openai",
            system_prompt="",
            messages=[],
            api_key="sk-secret-12345",
        )
        path = save_conversation_to_disk(conv, "special:coder")
        with open(path) as f:
            data = json.load(f)
        assert "api_key" not in data  # HIGH-3


class TestResolveSessionWorkspace:
    """resolve_session_workspace() per-session secure workspace."""

    def test_valid_session_key(self, tmp_path):
        ws = resolve_session_workspace(str(tmp_path), "special:coder")
        assert os.path.isdir(ws)

    def test_empty_project_path_raises(self):
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("", "special:coder")

    def test_path_escape_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace(str(tmp_path), "../escape")

    def test_colon_sanitized(self, tmp_path):
        ws = resolve_session_workspace(str(tmp_path), "special:coder")
        assert "special-coder" in ws  # colon → hyphen


class TestMigrateConversationFiles:
    """migrate_conversation_files() ONE-TIME migration HIGH-3."""

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        # First call may "migrate"; second must be no-op
        migrate_conversation_files()
        assert migrate_conversation_files() == 0


class TestSingletonPath:
    """Debugger's fold-in: the autouse fixture masks the production singleton
    path — prove the real _get_store() lazy-singleton path works (and where)."""

    def test_real_singleton_path_used_when_override_cleared(
        self, tmp_path, monkeypatch
    ):
        """_store_override=None + _store_singleton=None: the REAL _get_store()
        path runs, builds the singleton on the patched config dir, and REUSES
        the instance on a second call. Restored in the finally so no cross-test
        bleed (the autouse fixture's monkeypatch teardown alone can't restore
        the module globals it never set)."""
        import agent.persistence as persistence_mod

        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        orig_override = persistence_mod._store_override
        orig_singleton = persistence_mod._store_singleton
        try:
            persistence_mod._store_override = None
            persistence_mod._store_singleton = None

            from utils.transcript_store import TranscriptStore

            store1 = persistence_mod._get_store()
            assert isinstance(store1, TranscriptStore)
            store2 = persistence_mod._get_store()
            assert store1 is store2  # singleton identity
            # The default DB path resolves under the PATCHED config dir.
            assert store1._db_path == str(tmp_path / "transcript.db")
            # End-to-end through the real path: save + read back from the store.
            conv = Conversation(
                agent_name="Coder",
                model="openai/gpt-4o",
                messages=[Message(role=MessageRole.USER, content="via-singleton")],
            )
            save_conversation_to_disk(conv, "special:coder")
            # Read via a FRESH connection on the default-path DB (not store1's
            # handle) to prove where the singleton actually writes.
            check = TranscriptStore(db_path=str(tmp_path / "transcript.db"))
            turns = check.load_all("special:coder")
            assert [t["content"] for t in turns] == ["via-singleton"]
            check.close()
        finally:
            persistence_mod._store_override = orig_override
            persistence_mod._store_singleton = orig_singleton