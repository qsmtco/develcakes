# tests/test_config_migration.py — SPEC-11 SP1: migrate_v1_config() contract tests.
#
# RED-first: this file was written BEFORE migrate_v1_config existed in
# utils/config.py; all 9 tests failed at collection with ImportError.
#
# Contract under test (D3, docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md):
#   - Copy list: agent.json, providers.yaml, config.json, audit-log.jsonl,
#     feed-prefs.json, transcript.db (+ -wal/-shm sidecars) as files;
#     conversations/, agents/, projects/ as recursed directories.
#   - No-op guards (return None, nothing created): v1 dir absent; marker file
#     present; new dir already contains ANY copy-list entry (both-dirs case:
#     new wins silently).
#   - Verify: per-file byte counts post-copy; directories recursed per file.
#   - Failure: rollback deletes ONLY entries this migration created; v1 dir is
#     NEVER touched; NO marker; report {"copied": [], "skipped": [...],
#     "failed": [...]} (copied is [] because rollback removed everything).
#   - Success: marker written ONLY on full success; report has all three keys.
#
# Filesystem logic — mocks nothing. Fake XDG_CONFIG_HOME via monkeypatch;
# real tmp dirs; real files; failure injected via real filesystem shape
# (a directory where the source expects a regular file).

import hashlib
import os
import shutil

import pytest

from utils.config import (
    MIGRATION_MARKER,
    _copy_file_verified,
    get_config_dir,
    get_v1_config_dir,
    migrate_v1_config,
)

# The full D3 copy list, split the way the implementation sees it.
FULL_FILES = (
    "agent.json",
    "providers.yaml",
    "config.json",
    "audit-log.jsonl",
    "feed-prefs.json",
    "transcript.db",
    "transcript.db-wal",
    "transcript.db-shm",
)
FULL_DIRS = ("conversations", "agents", "projects")
FULL_ENTRIES = set(FULL_FILES) | set(FULL_DIRS)


def _content(entry: str) -> bytes:
    """Deterministic, per-entry-distinct content (distinct sizes catch
    cross-entry copy bugs — a copied-from-wrong-source bug cannot pass)."""
    sizes = {
        "agent.json": 111,
        "providers.yaml": 222,
        "config.json": 333,
        "audit-log.jsonl": 44,
        "feed-prefs.json": 55,
        "transcript.db": 6666,
        "transcript.db-wal": 77,
        "transcript.db-shm": 88,
    }
    base = f"content-of-{entry}".encode()
    return (base * 8)[: sizes.get(entry, 99)]


def _seed_v1(xdg, files=FULL_FILES, dirs=FULL_DIRS, nested=False):
    """Create the v1 (crabcakes) config dir under the fake XDG root.

    BUG#8: the default shape is FLAT — files directly inside each dir, the
    real v1 shape. The original seeder always fabricated nested/ and masked
    the flat-dir copy bug. nested=True seeds the nested variant; the suite
    asserts BOTH shapes.
    """
    v1 = xdg / "crabcakes"
    v1.mkdir()
    for name in files:
        p = v1 / name
        p.write_bytes(_content(name))
    for d in dirs:
        (v1 / d).mkdir()
        (v1 / d / "top.txt").write_bytes(f"top-of-{d}".encode())
        if nested:
            (v1 / d / "nested").mkdir()
            (v1 / d / "nested" / "deep.txt").write_bytes(f"deep-of-{d}".encode())
    return v1


def _tree_state(root):
    """Snapshot a directory tree: {relpath: ("file", size, sha256, mtime_ns)}
    for files, {relpath: ("dir",)} for dirs. Catches content, size, AND
    new/deleted entry changes."""
    state = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        rel_base = os.path.relpath(dirpath, str(root))
        for d in dirnames:
            rel = os.path.normpath(os.path.join(rel_base, d))
            state[rel] = ("dir",)
        for f in filenames:
            p = os.path.join(dirpath, f)
            rel = os.path.normpath(os.path.join(rel_base, f))
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            state[rel] = ("file", os.path.getsize(p), digest, os.stat(p).st_mtime_ns)
    return state


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Fake XDG_CONFIG_HOME root — both v1 and new dirs live under it."""
    root = tmp_path / "xdg-home"
    root.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    return root


class TestMigrateV1Config:
    @pytest.mark.parametrize("nested", [False, True])
    def test_migration_copies_all_entries(self, xdg, nested):
        """Full copy list present in v1 → every entry lands in the new dir,
        byte counts verified equal, marker written, report copied == full list,
        skipped and failed empty. Parametrized over BOTH dir shapes (BUG#8):
        flat (the real v1 shape) and nested (deep.txt under nested/)."""
        _seed_v1(xdg, nested=nested)
        new_dir = get_config_dir()
        assert get_v1_config_dir().endswith("crabcakes")

        report = migrate_v1_config()

        assert report is not None
        assert set(report["copied"]) == FULL_ENTRIES
        assert report["skipped"] == []
        assert report["failed"] == []
        # Every entry present in the new dir with identical byte counts.
        for name in FULL_FILES:
            src = os.path.join(str(xdg), "crabcakes", name)
            dst = os.path.join(new_dir, name)
            assert os.path.isfile(dst), name
            assert os.path.getsize(dst) == os.path.getsize(src), name
        deep_rels = ("top.txt", os.path.join("nested", "deep.txt")) if nested else (
            "top.txt",
        )
        for d in FULL_DIRS:
            assert os.path.isdir(os.path.join(new_dir, d)), d
            for rel in deep_rels:
                s = os.path.join(str(xdg), "crabcakes", d, rel)
                dd = os.path.join(new_dir, d, rel)
                assert os.path.isfile(dd), (d, rel)
                assert os.path.getsize(dd) == os.path.getsize(s), (d, rel)
        assert os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))

    def test_migration_marker_prevents_rerun(self, xdg):
        """Second call after a successful migration returns None and leaves
        the new dir byte-for-byte unchanged (content, size, mtime)."""
        _seed_v1(xdg)
        first = migrate_v1_config()
        assert first is not None
        new_dir = get_config_dir()
        assert os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))
        before = _tree_state(new_dir)

        second = migrate_v1_config()

        assert second is None
        assert _tree_state(new_dir) == before

    def test_migration_non_destructive_to_v1(self, xdg):
        """The v1 dir is NEVER touched: every file's content hash, size, and
        mtime identical post-migration; no entry added (the marker must NOT
        be written into v1)."""
        v1 = _seed_v1(xdg)
        before = _tree_state(v1)

        report = migrate_v1_config()

        assert report is not None
        assert _tree_state(v1) == before

    def test_migration_fresh_install_no_v1(self, xdg):
        """No v1 dir at all → None, and the new config dir is NOT created
        (fresh installs go straight to the wizard, GAP-2)."""
        assert not os.path.isdir(get_v1_config_dir())

        report = migrate_v1_config()

        assert report is None
        assert not os.path.isdir(get_config_dir())

    def test_migration_both_dirs_content_new_wins(self, xdg):
        """New dir already holds a copy-list entry (config.json) → None,
        v1 untouched, new content unchanged, and the REST of the copy list is
        NOT copied either (the whole migration is suppressed — new wins)."""
        _seed_v1(xdg)
        new_dir = xdg / "develcakes"
        new_dir.mkdir()
        new_content = b'{"fresh": "install"}'
        (new_dir / "config.json").write_bytes(new_content)
        v1_before = _tree_state(xdg / "crabcakes")

        report = migrate_v1_config()

        assert report is None
        assert (new_dir / "config.json").read_bytes() == new_content
        assert not os.path.exists(new_dir / "agent.json")
        assert not os.path.exists(new_dir / MIGRATION_MARKER)
        assert _tree_state(xdg / "crabcakes") == v1_before

    def test_migration_partial_failure_rolls_back(self, xdg):
        """Corrupt v1 state — config.json exists as a DIRECTORY, not a regular
        file (real filesystem failure, no mocks) → failed report names it,
        every OTHER entry this migration wrote is rolled back (new dir has
        none of them), v1 untouched, and NO marker exists."""
        v1 = _seed_v1(xdg)
        # Corrupt the source: config.json becomes a directory with content.
        (v1 / "config.json").unlink()
        (v1 / "config.json").mkdir()
        (v1 / "config.json" / "junk").write_bytes(b"not a regular file")
        v1_before = _tree_state(v1)
        new_dir = get_config_dir()

        report = migrate_v1_config()

        assert report is not None
        assert report["failed"], "config.json-as-dir must be reported as failed"
        assert any("config.json" in f for f in report["failed"])
        assert report["copied"] == [], "rollback removed everything — copied is []"
        # Rollback ran: NONE of the migration's entries survive.
        for name in FULL_FILES:
            if name == "config.json":
                continue  # never successfully copied
            assert not os.path.exists(os.path.join(new_dir, name)), name
        for d in FULL_DIRS:
            assert not os.path.exists(os.path.join(new_dir, d)), d
        assert not os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))
        # v1 untouched — including the corrupt entry itself.
        assert _tree_state(v1) == v1_before

    def test_migration_skips_absent_entries(self, xdg):
        """v1 missing some copy-list entries → they are listed in skipped,
        the present ones are copied, marker written (partial v1 is still a
        migration-worthy v1)."""
        present_files = ("agent.json", "config.json", "transcript.db")
        present_dirs = ("conversations", "agents")
        _seed_v1(xdg, files=present_files, dirs=present_dirs)

        report = migrate_v1_config()

        assert report is not None
        assert set(report["copied"]) == set(present_files) | set(present_dirs)
        assert set(report["skipped"]) == FULL_ENTRIES - (
            set(present_files) | set(present_dirs)
        )
        assert report["failed"] == []
        assert os.path.isfile(os.path.join(get_config_dir(), MIGRATION_MARKER))

    def test_migration_transcript_sidecars(self, xdg):
        """transcript.db + -wal + -shm all present → all three copied and
        byte-count verified. The DB is copied as opaque bytes — the contract
        forbids opening it (a live WAL mid-checkpoint is fine for a copy)."""
        _seed_v1(xdg, files=("transcript.db", "transcript.db-wal",
                             "transcript.db-shm"), dirs=())

        report = migrate_v1_config()

        assert report is not None
        for name in ("transcript.db", "transcript.db-wal", "transcript.db-shm"):
            assert name in report["copied"], name
            src = os.path.join(str(xdg), "crabcakes", name)
            dst = os.path.join(get_config_dir(), name)
            assert os.path.isfile(dst)
            assert os.path.getsize(dst) == os.path.getsize(src)

    def test_migration_does_not_create_config_dir_on_noop(self, xdg):
        """No no-op guard path may create the new config dir.

        The only reachable no-op state with an ABSENT new dir is v1-absent
        (the marker lives INSIDE the new dir, so 'marker exists, new dir
        absent' is not a representable filesystem state — the marker check
        is exercised and returns False on the missing path, then the
        v1-absent guard fires). This pins the guard ORDERING: existence
        checks happen before any os.makedirs, so a fresh install never
        gets an empty develcakes dir conjured by the migration probe.
        """
        assert not os.path.isdir(get_v1_config_dir())
        assert not os.path.isdir(get_config_dir())

        report = migrate_v1_config()

        assert report is None
        assert not os.path.isdir(get_config_dir())

    def test_migration_failed_run_leaves_no_poison_marker(self, xdg):
        """Marker is written ONLY on full success. A failed migration must
        not leave a marker behind — otherwise the run after the user repairs
        their v1 dir becomes a silent no-op (poisoned one-time migration).

        RED-proofed by mutant M3: writing the marker before the copies left
        this marker present after a failed run; the original 9 missed it —
        none re-ran after a failure.
        """
        v1 = _seed_v1(xdg)
        (v1 / "config.json").unlink()
        (v1 / "config.json").mkdir()  # malformed source → failure
        new_dir = get_config_dir()

        first = migrate_v1_config()

        assert first is not None and first["failed"]
        assert not os.path.exists(os.path.join(new_dir, MIGRATION_MARKER)), (
            "a failed migration must not write the success marker"
        )
        # And the repaired retry actually migrates (the one-shot contract
        # applies to SUCCESS, not to failure).
        (v1 / "config.json").rmdir()
        (v1 / "config.json").write_bytes(_content("config.json"))
        second = migrate_v1_config()
        assert second is not None and second["copied"], (
            "retry after a failed migration must not be a silent no-op"
        )
        assert "config.json" in second["copied"]

    def test_migration_rollback_never_deletes_pre_existing_files(self, xdg):
        """Rollback deletes ONLY what the migration itself wrote. A
        pre-existing, non-copy-list file in the new dir (user created
        something there before first launch) must survive a rolled-back
        migration untouched. RED-proofed by mutant M4 (rmtree of the whole
        new dir): this pin failed against it.
        """
        _seed_v1(xdg)
        new_dir = xdg / "develcakes"
        new_dir.mkdir()
        (new_dir / "user-notes.txt").write_bytes(b"mine - do not delete")
        v1_config = xdg / "crabcakes" / "config.json"
        v1_config.unlink()
        v1_config.mkdir()  # malformed source → guaranteed failure

        report = migrate_v1_config()

        assert report is not None and report["failed"]
        assert (new_dir / "user-notes.txt").read_bytes() == b"mine - do not delete"

    def test_migration_dir_entry_as_regular_file_fails_and_rolls_back(self, xdg):
        """Malformed v1 shape in the DIRS loop: a copy-list directory that is
        actually a regular file → failure, everything copied before it rolls
        back (files AND dirs), no marker, v1 untouched. RED-proofed by mutant
        M6 (check dropped): the never-raise handler still converted the
        resulting NotADirectoryError, so the failure path itself held; this
        pin adds that the dirs-loop's own guard is what fires and that
        earlier-copied entries are rolled back.
        """
        v1 = _seed_v1(xdg, files=("agent.json", "config.json"),
                      dirs=("conversations",))
        (v1 / "agents").write_bytes(b"this should have been a directory")
        v1_before = _tree_state(v1)

        report = migrate_v1_config()

        assert report is not None
        # BUG#2 (round 3): EXACT equality — the failed report must be exactly
        # one element naming the in-flight entry. The doubled "agents: agents:"
        # is the honest shape (the malformed-source raises carry their own
        # `{name}: ` prefix; the catch-site label adds the entry name). The
        # exact pin is what catches the R2-3b mutant (raise-prefix dropped):
        # a substring assert would still match the label alone.
        assert report["failed"] == [(
            "agents: agents: copy-list dir entry is not a directory: "
            f"{os.path.join(str(v1), 'agents')}"
        )], report["failed"]
        assert report["copied"] == []
        new_dir = get_config_dir()
        for name in ("agent.json", "config.json", "conversations"):
            assert not os.path.exists(os.path.join(new_dir, name)), name
        assert not os.path.exists(os.path.join(new_dir, MIGRATION_MARKER))
        assert _tree_state(v1) == v1_before

    def test_migration_fifo_entry_fails_fast_and_does_not_hang(self, xdg):
        """A FIFO named like a copy-list entry must FAIL the migration, not
        block it: os.path.exists() is True for a FIFO, and a naive
        open(src, 'rb') on a FIFO blocks until a writer appears — the
        isfile guard is what turns that hang into a labeled failure.

        The call runs on a daemon thread with a hard timeout so a future
        regression FAILS this test with a clear message instead of hanging
        the whole suite. RED-proofed by mutant M6 (guard dropped): the
        thread never returned and this test failed at the is_alive pin.
        """
        import threading

        v1 = _seed_v1(xdg, files=("config.json",), dirs=())
        os.mkfifo(v1 / "agent.json")

        result: dict = {}

        def run():
            result["report"] = migrate_v1_config()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), (
            "migration blocked on a FIFO copy-list entry — the "
            "regular-file guard was dropped (open() on a FIFO waits "
            "forever for a writer)"
        )
        report = result.get("report")
        assert report is not None and report["failed"]
        # BUG#2 (round 3): EXACT equality — one element naming the in-flight
        # FIFO entry. The doubled prefix is honest (raise text + catch-site
        # label); exact equality catches the R2-3 mutant (raise-prefix
        # dropped), which a substring assert would miss (the label alone
        # contains "agent.json").
        assert report["failed"] == [(
            "agent.json: agent.json: copy-list entry is not a regular file: "
            f"{os.path.join(str(v1), 'agent.json')}"
        )], report["failed"]
        new_dir = get_config_dir()
        assert not os.path.exists(os.path.join(new_dir, "config.json"))
        assert not os.path.exists(os.path.join(new_dir, MIGRATION_MARKER))

    # ── SP1 fix round (2026-10-04): 6 auditor findings, RED-first ─────────

    def test_flat_conversations_dir_copies(self, xdg):
        """BUG#1 (CRITICAL): the REAL v1 shape is flat — s1.json/s2.json
        directly inside conversations/, no nested/ subdir. The original
        _copy_dir_verified only created destination dirs inside
        `for d in dirnames:`, so every flat file copy raised
        FileNotFoundError through `<dst>/conversations/./s1.json`.
        """
        v1 = _seed_v1(xdg, files=("agent.json",), dirs=("conversations",))
        (v1 / "conversations" / "s1.json").write_bytes(b"session-one")
        (v1 / "conversations" / "s2.json").write_bytes(b"session-two")

        report = migrate_v1_config()

        assert report is not None
        assert report["failed"] == []
        new_dir = get_config_dir()
        for name in ("s1.json", "s2.json"):
            dst = os.path.join(new_dir, "conversations", name)
            assert os.path.isfile(dst), name
            assert os.path.getsize(dst) == os.path.getsize(
                os.path.join(str(v1), "conversations", name)
            )
        assert os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))

    def test_partial_dir_failure_rolls_back_and_retries(self, xdg):
        """BUG#2 (HIGH): a mid-recursion dir failure must leave the
        destination dir GONE (rollback rmtrees the partial tree — the entry
        must be in `created` BEFORE the copy starts), no marker; and the
        repaired retry must migrate fully (guard-3 not poisoned by a
        leftover empty conversations/ skeleton).
        """
        v1 = _seed_v1(xdg, files=(), dirs=("conversations",))
        (v1 / "conversations" / "keep.json").write_bytes(b"survives the copy")
        os.mkfifo(v1 / "conversations" / "pipe.fifo")

        report = migrate_v1_config()

        assert report is not None and report["failed"]
        # BUG#2 (round 3): EXACT equality — the walk raise (expected-regular-
        # file on the FIFO) is labeled at the DIRS-LOOP catch site with the
        # in-flight dir entry. The walk message itself is unprefixed, so the
        # honest string carries ONE name prefix.
        assert report["failed"] == [(
            "conversations: expected regular file in "
            f"{os.path.join(str(v1), 'conversations')}: "
            f"{os.path.join(str(v1), 'conversations', 'pipe.fifo')}"
        )], report["failed"]
        assert not os.path.exists(
            os.path.join(get_config_dir(), "conversations")
        ), "partial dir must be rolled back entirely"
        assert not os.path.exists(os.path.join(get_config_dir(), MIGRATION_MARKER))
        # Repair → retry migrates.
        os.remove(v1 / "conversations" / "pipe.fifo")
        retry = migrate_v1_config()
        assert retry is not None and retry["copied"] == ["conversations"]
        assert os.path.isfile(
            os.path.join(get_config_dir(), "conversations", "keep.json")
        )
        assert os.path.isfile(os.path.join(get_config_dir(), MIGRATION_MARKER))

    def test_unreadable_v1_fails_loud_no_marker(self, xdg):
        """BUG#3 (HIGH): chmod-000 v1 dir must produce a FAILED report naming
        it — NEVER None, NEVER skipped=11+marker (the silent permanent
        stranding the auditor reproduced). Retry after chmod +r+x migrates.

        Runs as root-insensitive: skips if the chmod doesn't stick (root
        ignores permission bits), keeping the suite honest on CI containers.
        """
        _seed_v1(xdg)
        v1 = get_v1_config_dir()
        os.chmod(v1, 0o000)
        if os.access(v1, os.R_OK):
            os.chmod(v1, 0o755)
            pytest.skip("running as root — permission bits not enforced")

        report = migrate_v1_config()

        assert report is not None, "unreadable v1 must FAIL, not no-op"
        assert report["failed"], "failed report must name the unreadable dir"
        assert any("unreadable" in f for f in report["failed"])
        assert not os.path.exists(os.path.join(get_config_dir(), MIGRATION_MARKER))
        # Repair → retry migrates.
        os.chmod(v1, 0o755)
        retry = migrate_v1_config()
        assert retry is not None
        assert set(retry["copied"]) == FULL_ENTRIES

    def test_unreadable_subdir_inside_readable_v1_fails(self, xdg):
        """BUG#3 second half: an unreadable SUBDIR inside a readable v1 must
        fail the migration via the os.walk onerror path (never silently
        skip). Same root-insensitivity skip as above.
        """
        _seed_v1(xdg, files=("agent.json",), dirs=("conversations",))
        v1 = get_v1_config_dir()
        conv_dir = os.path.join(v1, "conversations")
        os.chmod(conv_dir, 0o000)
        if os.access(conv_dir, os.R_OK):
            os.chmod(conv_dir, 0o755)
            pytest.skip("running as root — permission bits not enforced")

        report = migrate_v1_config()

        assert report is not None and report["failed"]
        assert any("conversations" in f for f in report["failed"])
        assert not os.path.exists(os.path.join(get_config_dir(), MIGRATION_MARKER))
        os.chmod(conv_dir, 0o755)

    def test_empty_placeholder_dir_does_not_block(self, xdg):
        """BUG#4 / D3 REV 2: an EMPTY pre-existing dir in the new config dir
        is not content — migration PROCEEDS (the copy absorbs it). Only a
        file or a NON-EMPTY dir suppresses.
        """
        _seed_v1(xdg, files=("agent.json",), dirs=("conversations",))
        new_dir = get_config_dir()
        os.makedirs(os.path.join(new_dir, "conversations"))  # empty placeholder

        report = migrate_v1_config()

        assert report is not None, "empty placeholder must NOT suppress"
        assert "agent.json" in report["copied"]
        assert "conversations" in report["copied"]
        assert os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))

    def test_live_source_append_copies_cleanly(self, xdg):
        """BUG#5 (round 1): the verify reference is len(data) — the bytes WE
        READ — not a re-read of a possibly-mutated live source. A v1 file
        that grows after our read copies cleanly (dst == the read snapshot);
        with the old getsize(src) reference this raised a false mismatch.

        NOTE (round 2 correction): this test does NOT prove the verify FIRES
        on a short write — the previous docstring's M5-catchability claim was
        false (the whole file passed with `if False:` replacing the verify).
        The firing behavior is pinned by the unit test
        test_copy_file_verified_detects_short_write; this test pins only the
        len(data) REFERENCE (wrong-reference is detectable without fault
        injection; short-write is not).
        """
        v1 = _seed_v1(xdg, files=("config.json",), dirs=())
        src = v1 / "config.json"
        snapshot = None

        real_open = open

        def spying_open(file, mode="r", *args, **kwargs):
            fh = real_open(file, mode, *args, **kwargs)
            if os.path.abspath(str(file)) == os.path.abspath(str(src)) and "r" in mode:
                data = fh.read()
                fh.seek(0)
                # Live-append AFTER the migration's read, BEFORE its verify.
                with real_open(src, "ab") as appender:
                    appender.write(b"-appended-by-live-writer")
                nonlocal snapshot
                snapshot = data
                # Hand back a wrapper so the migration still reads cleanly.
                import io

                wrapped = io.BytesIO(data)
                wrapped.close = lambda: None  # context manager stays usable
                return wrapped
            return fh

        import builtins

        saved_open = builtins.open
        builtins.open = spying_open
        try:
            report = migrate_v1_config()
        finally:
            builtins.open = saved_open

        assert report is not None and report["failed"] == []
        dst = os.path.join(get_config_dir(), "config.json")
        with real_open(dst, "rb") as fh:
            copied = fh.read()
        assert snapshot is not None, "the read seam must have been observed"
        assert copied == snapshot, (
            "dst must equal the READ snapshot (len(data) reference), not "
            "diverge when the live source grows after the read"
        )

    def test_dangling_symlink_destination_rejected(self, xdg):
        """BUG#6: guard-3 uses lexists (a dangling link counts as present),
        and any symlinked copy destination is rejected fail-closed — the
        write must never escape the config dir through a planted link.
        """
        v1 = _seed_v1(xdg, files=("config.json",), dirs=())
        outside = xdg / "OUTSIDE-TARGET.txt"  # outside the config dir
        new_dir = get_config_dir()
        os.makedirs(new_dir)  # planted link needs its parent to exist
        os.symlink(outside, os.path.join(new_dir, "config.json"))  # dangling

        report = migrate_v1_config()

        # Either no-op (lexists guard fires — the link IS present) or a
        # failed report — but NEVER a write through the link.
        if report is not None:
            assert report["failed"], "symlink dst must fail closed, not 'succeed'"
        assert not outside.exists(), "no write may escape the config dir"
        assert not os.path.exists(os.path.join(new_dir, MIGRATION_MARKER))

        # Second shape: a symlinked copy-list DIRECTORY pointing at an EMPTY
        # outside dir passes guard-4 (empty placeholder) — the copy-level
        # islink guard must then refuse it. RED-proofed by mutant F6b
        # (dir-level islink guard dropped): the tree was copied THROUGH the
        # link into the outside dir.
        os.remove(os.path.join(new_dir, "config.json"))  # first link gone
        (v1 / "conversations").mkdir()  # v1 now HAS the dir → dirs loop runs
        (v1 / "conversations" / "s1.json").write_bytes(b"through-the-link?")
        outside_dir = xdg / "OUTSIDE-DIR"
        outside_dir.mkdir()
        link_path = os.path.join(new_dir, "conversations")
        os.symlink(outside_dir, link_path)

        report2 = migrate_v1_config()

        assert report2 is not None and report2["failed"], (
            "symlinked dir destination must fail closed"
        )
        assert any("conversations" in f for f in report2["failed"])
        assert os.listdir(outside_dir) == [], (
            "nothing may be copied through the link into the outside dir"
        )
        # The link itself must survive rollback untouched (os.remove on a
        # link removes the link, never its target).
        assert os.path.islink(link_path)

    # ── SP1 fix round 2 (2026-10-04): 3 closure-audit findings, RED-first ─

    def test_symlinked_subdir_followed(self, xdg):
        """BUG#1 (round 2, D3 REV 3): symlinked SUBDIRS are FOLLOWED —
        `conversations/archive -> <outside store>` copies the store's
        linked.json (matching the file-following behavior). The old
        followlinks=False listed the link in dirnames but never descended:
        linked.json silently absent, failed=[], MARKER WRITTEN.
        """
        store = xdg / "v1-store"
        store.mkdir()
        (store / "linked.json").write_bytes(b"from-the-linked-store")
        v1 = _seed_v1(xdg, dirs=("conversations",))
        os.symlink(store, v1 / "conversations" / "archive")

        report = migrate_v1_config()

        assert report is not None and report["failed"] == []
        new_dir = get_config_dir()
        linked = os.path.join(new_dir, "conversations", "archive", "linked.json")
        assert os.path.isfile(linked), "linked subdir content must be copied"
        assert os.path.getsize(linked) == len(b"from-the-linked-store")
        # Real (non-linked) files in the same dir still copied.
        real_dir = os.path.join(new_dir, "conversations")
        assert os.path.isfile(os.path.join(real_dir, "top.txt"))
        assert os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))

    def test_symlink_cycle_fails_closed(self, xdg):
        """BUG#1b (round 2): a symlink cycle (`a/link -> a`) must fail the
        migration closed at the FIRST realpath revisit — failed report with
        the guard's own "symlink cycle detected" message, NO marker, and the
        call returns.

        Empirical note (round 2): followlinks=True does NOT hang forever on
        this shape — os.walk descends ~40 symlink levels until the kernel
        ELOOPs, then fails with a confusing deep-path error. The guard's
        value is failing FAST at the first revisit with a named cycle, and
        covering cycle shapes where ELOOP alone would not fire (mutual
        a->b->a walks the same two real dirs forever without ELOOP). The
        message pin is deliberately "symlink cycle detected" (spaced): the
        pytest tmp dir embeds this test's underscored name, which would
        otherwise satisfy a naive "cycle" substring assert under the
        guard-less mutant (RED-proofed: the guardless mutant fails exactly
        this assert).
        """
        v1 = _seed_v1(xdg, dirs=("conversations",))
        (v1 / "conversations" / "a").mkdir()
        os.symlink(v1 / "conversations" / "a", v1 / "conversations" / "a" / "link")

        result: dict = {}

        import threading

        t = threading.Thread(
            target=lambda: result.update(report=migrate_v1_config()),
            daemon=True,
        )
        t.start()
        t.join(timeout=15)
        assert not t.is_alive(), "migration hung on a symlink cycle"

        report = result.get("report")
        assert report is not None and report["failed"]
        assert any("symlink cycle detected" in f for f in report["failed"]), (
            report["failed"]
        )
        assert any("conversations" in f for f in report["failed"])
        assert not os.path.exists(os.path.join(get_config_dir(), MIGRATION_MARKER))

    def test_empty_dir_at_file_name_noop(self, xdg):
        """BUG#2 (round 2, D3 REV 3): an EMPTY dir at a FILE copy-list name
        (`new/config.json/`) IS content — the migration is a no-op (None).
        The old carve-out let it through to a permanent IsADirectoryError
        loop: failed report every run, no marker, no recovery.
        """
        _seed_v1(xdg, files=("config.json",), dirs=())
        new_dir = get_config_dir()
        os.makedirs(os.path.join(new_dir, "config.json"))  # empty dir at file name

        report = migrate_v1_config()

        assert report is None, "any entry at a FILE name is content → no-op"
        assert not os.path.exists(os.path.join(new_dir, MIGRATION_MARKER))

    def test_copy_file_verified_detects_short_write(self, monkeypatch, tmp_path):
        """BUG#3 (round 2): the byte-count verify must actually fire.

        Unit pin (auditor-ruled exempt from the no-mocks rule): monkeypatch
        os.path.getsize to lie about dst — the observable of a short write
        under ENOSPC. With the verify present this RAISES through the
        migration's never-raise handler (failed report names config.json);
        with the verify disabled (if False:) this test FAILS — the old
        M5-catchability claim in test_live_source_append_copies_cleanly's
        docstring was false and has been corrected to point here.
        """
        src = tmp_path / "src.json"
        dst = tmp_path / "dst.json"
        src.write_bytes(b"x" * 100)

        # The two destinations the lie applies to: the unit-pin dst and the
        # migration's own <new_dir>/config.json.
        real_getsize = os.path.getsize
        lying = {os.path.abspath(str(dst))}

        def lying_getsize(p):
            return 0 if os.path.abspath(str(p)) in lying else real_getsize(p)

        monkeypatch.setattr(os.path, "getsize", lying_getsize)

        with pytest.raises(OSError, match="byte-count mismatch"):
            _copy_file_verified(str(src), str(dst))

        # And through the migration path: the failed report names the entry.
        xdg_root = tmp_path / "xdg"
        xdg_root.mkdir()
        v1 = xdg_root / "crabcakes"
        v1.mkdir()
        (v1 / "config.json").write_bytes(b"y" * 50)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
        lying.add(os.path.abspath(os.path.join(get_config_dir(), "config.json")))

        report = migrate_v1_config()
        assert report is not None and report["failed"]
        assert any("config.json" in f for f in report["failed"])

    # ── SP1 fix round 3 (2026-10-04): chain-scoped cycle guard + honest
    #    failed-report labeling, RED-first ─────────────────────────────────

    def test_diamond_links_migrate(self, xdg):
        """BUG#1 (round 3, HIGH): a DIAMOND (`a -> store`, `b -> store`, two
        links sharing one target) is a legitimate DAG, not a cycle. The
        round-2 GLOBAL seen-set guard flagged the store's realpath on the
        second arrival and aborted the whole migration ('symlink cycle
        detected'), copying NOTHING. Chain-scoped detection: each walked
        dirpath's ancestor chain from the copy-list SOURCE is linear, so
        both links migrate and `shared.json` lands under BOTH names.
        """
        store = xdg / "v1-store"
        store.mkdir()
        (store / "shared.json").write_bytes(b"shared-by-both-links")
        v1 = _seed_v1(xdg, dirs=("conversations",))
        conv = v1 / "conversations"
        (conv / "a").mkdir()
        (conv / "b").mkdir()
        os.symlink(store, conv / "a" / "store")
        os.symlink(store, conv / "b" / "store")

        report = migrate_v1_config()

        assert report is not None and report["failed"] == [], report["failed"]
        new_dir = get_config_dir()
        for link in (("conversations", "a", "store"),
                     ("conversations", "b", "store")):
            dst = os.path.join(new_dir, *link, "shared.json")
            assert os.path.isfile(dst), link
            assert os.path.getsize(dst) == len(b"shared-by-both-links"), link
        assert os.path.isfile(os.path.join(new_dir, "conversations", "top.txt"))
        assert os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))

    def test_symlink_chain_migrates(self, xdg):
        """BUG#1 companion shape: a linear CHAIN (`a -> b -> c -> store`) has
        no repeat within any single ancestor chain — every walked dirpath is
        reached through a fresh set of components. The round-2 global seen-set
        flagged the shared target's realpath and aborted; chain-scoped
        detection walks each chain exactly once and migrates the store's
        content under `a/`.
        """
        store = xdg / "v1-store"
        store.mkdir()
        (store / "chained.json").write_bytes(b"end-of-the-chain")
        v1 = _seed_v1(xdg, dirs=("conversations",))
        conv = v1 / "conversations"
        (conv / "a").mkdir()
        (conv / "b").mkdir()
        (conv / "c").mkdir()
        (conv / "c" / "store").symlink_to(store)
        (conv / "b" / "c").symlink_to(conv / "c")
        (conv / "a" / "b").symlink_to(conv / "b")

        report = migrate_v1_config()

        assert report is not None and report["failed"] == [], report["failed"]
        new_dir = get_config_dir()
        dst = os.path.join(new_dir, "conversations", "a", "b", "c", "store",
                           "chained.json")
        assert os.path.isfile(dst), "chain content must migrate under a/"
        assert os.path.getsize(dst) == len(b"end-of-the-chain")
        assert os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER))

    def test_mutual_cycle_fails_closed(self, xdg):
        """The guard must still catch a TRUE cycle: `a/link -> b`,
        `b/link -> a` — every ancestor chain repeats a component's realpath,
        so the migration fails closed (failed report naming conversations and
        the cycle), NO marker, and the call RETURNS (mutual cycles walk the
        same two real dirs forever without the kernel's ELOOP — only the
        chain guard stops it).
        """
        v1 = _seed_v1(xdg, dirs=("conversations",))
        conv = v1 / "conversations"
        (conv / "a").mkdir()
        (conv / "b").mkdir()
        os.symlink(conv / "b", conv / "a" / "link")
        os.symlink(conv / "a", conv / "b" / "link")

        result: dict = {}

        import threading

        t = threading.Thread(
            target=lambda: result.update(report=migrate_v1_config()),
            daemon=True,
        )
        t.start()
        t.join(timeout=15)
        assert not t.is_alive(), "migration hung on a mutual symlink cycle"

        report = result.get("report")
        assert report is not None and report["failed"]
        assert any("symlink cycle detected" in f for f in report["failed"]), (
            report["failed"]
        )
        assert any("conversations" in f for f in report["failed"])
        assert not os.path.exists(os.path.join(get_config_dir(), MIGRATION_MARKER))

    def test_failed_report_names_failing_entry(self, xdg, monkeypatch):
        """BUG#2 (round 3, MED): report["failed"] must label the failure at
        the CATCH site with the IN-FLIGHT entry and its OWN exception —
        exactly one element. The old code relabeled the created list at the
        END (naming prior SUCCESSES) and invented a `migrate_v1_config:`
        pseudo-entry. Both shapes below RED today for the wrong-name reason;
        both pin the honest doubling note for malformed-source raises: those
        two raises carry their own `{name}: ` prefix, so the honest string is
        doubled (`agents: agents: ...`) — exact equality is what separates
        this pin from the R2-3/R2-3b prefix mutants.
        """
        # Shape (a): FIFO at agent.json — FIRST copy-list entry; nothing has
        # been created yet, so the old relabeling fell back to the
        # `migrate_v1_config:` pseudo-name (R2-4 mutant) and the comprehension
        # alone cannot name agent.json.
        v1 = _seed_v1(xdg, files=("config.json",), dirs=())
        os.mkfifo(v1 / "agent.json")

        report = migrate_v1_config()

        assert report is not None
        assert report["failed"] == [(
            "agent.json: agent.json: copy-list entry is not a regular file: "
            f"{os.path.join(str(v1), 'agent.json')}"
        )], report["failed"]

        # Shape (b): verify mismatch on providers.yaml via the getsize-lie
        # seam (same seam as the round-2 unit pin, extended through the
        # migration path): agent.json copies FIRST and SUCCEEDS, so the old
        # end-of-run relabeling reported "agent.json" (a prior SUCCESS) with
        # providers.yaml's exception. The catch-site label must name
        # providers.yaml — the entry that actually failed.
        os.remove(v1 / "agent.json")  # shape (a)'s FIFO
        new_dir = get_config_dir()
        if os.path.isdir(new_dir):
            shutil.rmtree(new_dir)  # shape (a) rolled back; start clean
        (v1 / "agent.json").write_bytes(_content("agent.json"))
        (v1 / "providers.yaml").write_bytes(_content("providers.yaml"))
        real_getsize = os.path.getsize
        lying = {os.path.abspath(os.path.join(new_dir, "providers.yaml"))}

        def lying_getsize(p):
            return 0 if os.path.abspath(str(p)) in lying else real_getsize(p)

        monkeypatch.setattr(os.path, "getsize", lying_getsize)
        report = migrate_v1_config()

        assert report is not None
        assert report["failed"] == [(
            "providers.yaml: byte-count mismatch after copy: "
            f"{os.path.join(str(v1), 'providers.yaml')} -> "
            f"{os.path.join(new_dir, 'providers.yaml')}"
        )], report["failed"]
        assert not any("agent.json" in f for f in report["failed"])
        assert not os.path.exists(os.path.join(new_dir, MIGRATION_MARKER))

    # ── SP1 fix round 4 (2026-10-04): destination-reentry guard + atomic
    #    marker, RED-first ────────────────────────────────────────────────

    def test_destination_reentry_fails_fast(self, xdg):
        """BUG#4 (round 4, MED, pre-existing): a v1 symlink pointing INTO the
        destination config dir (`conversations/loop -> <new_dir>` — dangling
        at plant time, valid once the migration creates the dir) drove
        SELF-AMPLIFYING recursion: followlinks=True descends into the
        destination being written, every level makedirs a fresh dir, every
        dirpath's chain is genuinely new — the chain guard never fires. The
        auditor's probe ran 94.8s to ENAMETOOLONG with ~600 nested dirs
        written into the user's config dir.

        Forbidden-root rejection: any walked dirpath whose realpath IS the
        new config dir or lands under it raises `copy destination
        re-entered` — bounded (fires on FIRST reentry), covers the whole
        class (link into own dst, into the config root, into ANOTHER entry's
        dst). No false positive: legit walked paths are under the v1 src,
        never under the new config dir — only a bridging symlink gets you
        there, which IS the bug.

        RED (pre-fix): the migration hangs past 5s (probe: alive at 12s with
        357 entries). GREEN (post-fix): named failure in <5s, NO marker, and
        post-rollback the new dir holds none of the nested loop dirs.
        """
        v1 = _seed_v1(xdg, dirs=("conversations",))
        new_dir = get_config_dir()
        os.symlink(new_dir, v1 / "conversations" / "loop")  # dangling at plant

        result: dict = {}

        import threading

        t = threading.Thread(
            target=lambda: result.update(report=migrate_v1_config()),
            daemon=True,
        )
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), (
            "migration ran away recursing into its own destination — the "
            "forbidden-root guard was dropped (auditor probe: 94.8s to "
            "ENAMETOOLONG, ~600 nested dirs written into the config dir)"
        )

        report = result.get("report")
        assert report is not None and report["failed"]
        assert any("copy destination re-entered" in f for f in report["failed"]), (
            report["failed"]
        )
        assert any("conversations" in f for f in report["failed"])
        assert not os.path.exists(os.path.join(new_dir, MIGRATION_MARKER))
        # Rollback discipline held: no nested loop dirs survived in the new
        # config dir. `conversations` was created-before-copy, so rollback
        # rmtree'd the whole partial tree — any surviving `loop` dir here
        # would mean the rollback missed the recursion's output.
        assert not os.path.exists(os.path.join(new_dir, "conversations"))
        # Rollback rmdirs the conjured dir when it leaves it empty — the dir
        # being GONE is the canonical clean-rollback outcome; if it somehow
        # survived, it must hold nothing (either way: no recursion debris).
        assert not os.path.isdir(new_dir) or os.listdir(new_dir) == [], (
            f"recursion debris left in the config dir: {os.listdir(new_dir)}"
        )

    def test_marker_write_failure_retries_clean(self, xdg):
        """BUG#5 (round 4, LOW-MED, pre-existing round-1 logic): open(marker,
        'w') CREATES the file; a write() failure (ENOSPC) left a 0-byte
        marker that survived rollback (the marker is never in `created`) —
        every future run was a silent permanent no-op, stranding the user's
        data behind a marker that claims a migration that never finished.

        Atomic marker: write to `marker.tmp` then os.replace — a failed
        write can only leave the TMP (cleaned in the except block together
        with any partial marker). RED (pre-fix): 0-byte marker present after
        the failure and the repaired retry is a silent no-op (None).

        Seam note: builtins.open is swapped MANUALLY (try/finally, the
        house pattern from test_live_source_append_copies_cleanly) — NOT
        monkeypatch.setattr, because monkeypatch.undo() mid-test would also
        revert the xdg fixture's XDG_CONFIG_HOME and the retry would probe
        the real config dir.
        """
        import builtins as builtins_mod
        import threading

        _seed_v1(xdg)
        new_dir = get_config_dir()
        marker = os.path.join(new_dir, MIGRATION_MARKER)

        # The unit-pin seam: patch the marker handle's write to raise after
        # open — open('w') has already created the (empty) TMP file. The tmp
        # ONLY is poisoned, deliberately: with the atomic shape the marker
        # write IS the tmp write, so the failure fires; a non-atomic revert
        # (open(marker) + write, no replace) would write the unpoisoned final
        # path and SUCCEED — tripping the failed-report assert below. Poison
        # the final path too and the atomicity pin goes blind (both shapes
        # fail identically; cleanup masks the difference).
        real_open = builtins_mod.open

        def failing_marker_open(file, mode="r", *args, **kwargs):
            fh = real_open(file, mode, *args, **kwargs)
            if (mode.startswith("w") and os.path.abspath(str(file))
                    == os.path.abspath(marker + ".tmp")):
                fh.write = lambda *a, **k: (_ for _ in ()).throw(
                    OSError(28, "No space left on device")
                )
            return fh

        builtins_mod.open = failing_marker_open
        result: dict = {}
        try:
            t = threading.Thread(
                target=lambda: result.update(report=migrate_v1_config()),
                daemon=True,
            )
            t.start()
            t.join(timeout=10)
        finally:
            builtins_mod.open = real_open
        assert not t.is_alive(), "marker-failure migration hung"

        report = result.get("report")
        assert report is not None and report["failed"]
        assert not os.path.exists(marker), (
            "a 0-byte marker survived the failure — the one-shot is "
            "poisoned: every future run is a silent no-op"
        )
        assert not os.path.exists(marker + ".tmp"), "tmp marker survived"
        # Cleanup + rollback dropped everything — the conjured dir being GONE
        # is the canonical clean-rollback outcome (cleanup ran BEFORE the
        # rmdir so an emptied dir is actually dropped).
        assert not os.path.isdir(new_dir) or os.listdir(new_dir) == [], (
            f"failure debris left in the config dir: {os.listdir(new_dir)}"
        )

        # Repaired retry migrates fully — the one-shot applies to SUCCESS,
        # not to a poisoned failure.
        retry = migrate_v1_config()
        assert retry is not None and set(retry["copied"]) == FULL_ENTRIES
        assert os.path.isfile(marker) and os.path.getsize(marker) > 0


class TestMigrationPreservesPermissions:
    """SPEC-11 live-test regression: migrate_v1_config() must PRESERVE source
    file modes. v1's key-bearing files are 0600 (agent.json, providers.yaml
    per agent/config.py; transcript.db per SPEC-08). open(dst, "wb") creates
    at the process umask — silently LOOSENING 0600 to 0644/0664, a security
    regression on the one-time copy (found in live manual testing)."""

    def test_key_files_keep_0600(self, xdg):
        from utils.config import migrate_v1_config

        v1 = xdg / "crabcakes"
        v1.mkdir()
        for name in ("agent.json", "providers.yaml"):
            p = v1 / name
            p.write_bytes(b"key-material\n")
            os.chmod(p, 0o600)
        # transcript.db triple, 0600
        for name in ("transcript.db", "transcript.db-wal", "transcript.db-shm"):
            p = v1 / name
            p.write_bytes(b"dbbytes")
            os.chmod(p, 0o600)

        migrate_v1_config()

        new = xdg / "develcakes"
        for name in ("agent.json", "providers.yaml",
                     "transcript.db", "transcript.db-wal", "transcript.db-shm"):
            mode = os.stat(new / name).st_mode & 0o777
            assert mode == 0o600, (
                f"{name} migrated with mode {oct(mode)} — key-bearing file "
                f"must stay 0600 (source was 0600)"
            )

    def test_arbitrary_mode_preserved(self, xdg):
        """Not just 0600 — whatever the source mode is, the copy matches it."""
        from utils.config import migrate_v1_config

        v1 = xdg / "crabcakes"
        v1.mkdir()
        p = v1 / "agent.json"
        p.write_bytes(b"x\n")
        os.chmod(p, 0o640)
        migrate_v1_config()
        assert os.stat(xdg / "develcakes" / "agent.json").st_mode & 0o777 == 0o640
