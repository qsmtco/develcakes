# tests/test_work_persistence.py
# Coverage for utils/work_persistence.py — work.json source of truth,
# generated tasks.md summary, and legacy tasks.md migration (spec §3, Phase 2).
# SPEC-09 SP1 extends coverage to the work-lease API (claim/release/assert,
# TTL read-time expiry, D3 in-record storage).

import json
import logging
import os
import threading

import pytest

from models.work_unit import WorkUnit, WorkUnitStore
from utils.work_persistence import (
    DEFAULT_LEASE_TTL_SECONDS,
    SOURCE_OF_TRUTH_NOTE,
    assert_lease,
    claim_work,
    load_or_migrate_work_units,
    load_work_units,
    release_work,
    render_tasks_summary,
    save_work_units,
    tasks_summary_path,
    work_json_path,
    write_tasks_summary,
)

# ── Path helpers ─────────────────────────────────────────────────────────────

def test_work_json_path(tmp_path):
    assert work_json_path(str(tmp_path)) == os.path.join(
        str(tmp_path), ".crabcakes", "work.json"
    )


def test_tasks_summary_path(tmp_path):
    assert tasks_summary_path(str(tmp_path)) == os.path.join(
        str(tmp_path), ".crabcakes", "tasks.md"
    )


# ── JSON round-trip ──────────────────────────────────────────────────────────

def test_round_trip_preserves_all_fields(tmp_path):
    project = str(tmp_path)
    units = [
        WorkUnit(
            id="00000001",
            title="Build spec engine",
            spec_path="docs/specs/SPEC-engine.md",
            status="spec-ready",
            assigned_supervisor="special:supervisor",
            assigned_builder="special:coder",
            assigned_auditor="special:debugger",
            priority="high",
            depends_on=["00000002"],
            created_at="2026-07-31T10:00:00",
            updated_at="2026-07-31T10:05:00",
            completed_at="",
            post_mortem_path="",
            blocked_reason="",
        ),
        WorkUnit(
            id="00000002",
            title="Implement parser",
            status="draft",
            priority="low",
        ),
    ]
    save_work_units(project, units)
    loaded = load_work_units(project)

    assert [w.to_dict() for w in loaded] == [w.to_dict() for w in units]


def test_round_trip_depends_on_and_empty_strings(tmp_path):
    project = str(tmp_path)
    unit = WorkUnit(
        id="00000010",
        title="t",
        spec_path="",
        status="in-progress",
        depends_on=["00000001", "00000009"],
        created_at="",
        updated_at="",
        completed_at="",
        post_mortem_path="",
        blocked_reason="",
    )
    save_work_units(project, [unit])
    loaded = load_work_units(project)
    assert loaded[0].depends_on == ["00000001", "00000009"]
    assert loaded[0].spec_path == ""
    assert loaded[0].created_at == ""
    assert loaded[0].completed_at == ""
    assert loaded[0].post_mortem_path == ""
    assert loaded[0].blocked_reason == ""


def test_counter_advances_past_loaded_ids(tmp_path):
    project = str(tmp_path)
    save_work_units(project, [
        WorkUnit(id="00000050", title="a"),
        WorkUnit(id="00000075", title="b"),
    ])
    load_work_units(project)
    fresh = WorkUnit()  # default_factory must continue past max loaded (75)
    assert int(fresh.id) > 75


# ── Missing / invalid JSON (sad path) ────────────────────────────────────────

def test_missing_file_returns_empty_no_file_created(tmp_path):
    project = str(tmp_path)
    assert load_work_units(project) == []
    # Load must NOT create .crabcakes/ or work.json
    assert not os.path.exists(os.path.join(project, ".crabcakes", "work.json"))


def test_invalid_json_returns_empty_with_warning(tmp_path, caplog):
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        f.write("{ not valid json !!!")

    with caplog.at_level(logging.WARNING, logger="utils.work_persistence"):
        assert load_work_units(project) == []
    assert "work.json" in caplog.text


def test_load_work_units_binary_work_json_no_crash(tmp_path, caplog):
    """Binary/non-UTF8 work.json must not crash the load path: the text-mode
    open raises UnicodeDecodeError, which is NOT a json.JSONDecodeError or
    OSError, so it escaped the handler and aborted project open.

    Regression for BUG #9 (HIGH) — sibling of the BUG #1 fix on the tasks.md
    path. errors='replace' decodes the garbage to U+FFFD chars, json.load then
    raises json.JSONDecodeError (already caught), and the function returns
    None -> caller returns [] with a logged warning (spec §3.1 'never crash
    project open').
    """
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    with open(os.path.join(crab, "work.json"), "wb") as f:
        f.write(b"\x80\x81\x82")

    with caplog.at_level(logging.WARNING, logger="utils.work_persistence"):
        assert load_work_units(project) == []  # must not raise
    assert "work.json" in caplog.text


def test_wrong_shape_missing_work_units_returns_empty(tmp_path, caplog):
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1}, f)  # no work_units key

    with caplog.at_level(logging.WARNING, logger="utils.work_persistence"):
        assert load_work_units(project) == []


def test_wrong_shape_work_units_not_list_returns_empty(tmp_path, caplog):
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "work_units": {"00000001": {}}}, f)

    with caplog.at_level(logging.WARNING, logger="utils.work_persistence"):
        assert load_work_units(project) == []


def test_valid_empty_store_returns_empty(tmp_path):
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "work_units": []}, f)

    assert load_work_units(project) == []


def test_malformed_record_skipped_good_records_load(tmp_path, caplog):
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    payload = {
        "version": 1,
        "work_units": [
            {"id": "00000001", "title": "good", "status": "done"},
            {"id": 123, "title": "bad type"},          # non-string id -> ValueError
            {"id": "00000003", "status": "bogus-status"},  # bad status -> ValueError
        ],
    }
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)

    with caplog.at_level(logging.WARNING, logger="utils.work_persistence"):
        loaded = load_work_units(project)

    assert [w.id for w in loaded] == ["00000001"]
    assert loaded[0].title == "good"
    assert "malformed" in caplog.text or "skipping" in caplog.text


# ── Atomic save / summary failure isolation ──────────────────────────────────

def test_atomic_save_summary_failure_preserves_json(tmp_path, monkeypatch, caplog):
    """work.json must be durably written BEFORE the summary; a summary
    failure must not corrupt/delete/roll back the JSON source of truth."""
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)

    # Sentinel: proves the atomic replace actually happened (sentinel replaced
    # by valid JSON), and that the JSON survived the summary failure.
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        f.write("SENTINEL")

    def boom(*args, **kwargs):
        raise ValueError("summary render exploded")

    monkeypatch.setattr("utils.work_persistence.render_tasks_summary", boom)
    unit = WorkUnit(id="00000001", title="durable", priority="high")

    with caplog.at_level(logging.ERROR, logger="utils.work_persistence"):
        save_work_units(project, [unit])

    # work.json: sentinel gone, valid JSON with our unit, no leftover .tmp
    with open(os.path.join(crab, "work.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] == 1
    assert data["work_units"][0]["id"] == "00000001"
    assert not os.path.exists(os.path.join(crab, "work.json.tmp"))
    # The summary failure was logged, not swallowed silently
    assert "summary write failed" in caplog.text


def test_save_creates_crabcakes_dir(tmp_path):
    project = str(tmp_path)  # no .crabcakes yet
    save_work_units(project, [WorkUnit(id="00000001", title="x")])
    assert os.path.isfile(os.path.join(project, ".crabcakes", "work.json"))
    assert os.path.isfile(os.path.join(project, ".crabcakes", "tasks.md"))


# ── Deterministic summary ────────────────────────────────────────────────────

def test_render_tasks_summary_deterministic(tmp_path):
    units = [
        WorkUnit(id="00000002", title="B", created_at="2026-07-31T10:00:00"),
        WorkUnit(id="00000001", title="A", created_at="2026-07-30T09:00:00"),
    ]
    first = render_tasks_summary(units)
    second = render_tasks_summary(list(reversed(units)))
    assert first == second  # iterable ordering must not matter


def test_render_tasks_summary_header_and_spec_indicator():
    units = [
        WorkUnit(
            id="00000001",
            title="Has spec",
            spec_path="docs/specs/SPEC-x.md",
            status="spec-ready",
            priority="high",
        ),
        WorkUnit(id="00000002", title="No spec", status="draft", priority="low"),
    ]
    text = render_tasks_summary(units)
    assert "# Work Units" in text
    assert SOURCE_OF_TRUTH_NOTE in text
    # spec indicator distinguishes missing (⚠) vs present (✓)
    assert "⚠ no spec" in text
    assert "✓ docs/specs/SPEC-x.md" in text
    # Every unit appears with ID + title + status + priority + assignments
    for unit in units:
        assert unit.id in text
        assert unit.title in text
        assert unit.assigned_supervisor in text
        assert unit.assigned_builder in text
        assert unit.assigned_auditor in text
    # status line renders the human label (not the raw status string)
    assert "Spec Ready" in text
    assert "Draft" in text


# ── Generated-summary non-readback ───────────────────────────────────────────

def test_load_reads_only_work_json_not_tasks_md(tmp_path):
    project = str(tmp_path)
    save_work_units(project, [WorkUnit(id="00000001", title="from json")])

    # Mutate tasks.md — load_work_units must ignore it entirely
    with open(tasks_summary_path(project), "w", encoding="utf-8") as f:
        f.write("## Task 99999999: forged — 🔄 in_progress\n")
    loaded = load_work_units(project)
    assert [w.id for w in loaded] == ["00000001"]
    assert loaded[0].title == "from json"

    # Mutate again to a completely different value — still no change
    with open(tasks_summary_path(project), "w", encoding="utf-8") as f:
        f.write("garbage that is not even markdown")
    assert [w.id for w in load_work_units(project)] == ["00000001"]


# ── write_tasks_summary best-effort ─────────────────────────────────────────

def test_write_tasks_summary_creates_dir_and_file(tmp_path):
    project = str(tmp_path)
    write_tasks_summary(project, [WorkUnit(id="00000001", title="t")])
    assert os.path.isfile(tasks_summary_path(project))
    with open(tasks_summary_path(project), "r", encoding="utf-8") as f:
        assert SOURCE_OF_TRUTH_NOTE in f.read()


def test_write_tasks_summary_oserror_logged_no_raise(tmp_path, monkeypatch, caplog):
    project = str(tmp_path)

    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr("utils.work_persistence._atomic_write_text", boom)
    with caplog.at_level(logging.ERROR, logger="utils.work_persistence"):
        # best-effort: must NOT raise
        write_tasks_summary(project, [WorkUnit(id="00000001")])
    assert "failed to write" in caplog.text
    assert not os.path.exists(tasks_summary_path(project))


def test_write_tasks_summary_dotcrabcakes_is_file(tmp_path, caplog):
    """Corrupt project state (.crabcakes is a regular file) must not crash
    write_tasks_summary: _ensure_crabcakes_dir raises RuntimeError, which is
    logged and swallowed (spec §3.1 'never crash project open').

    Regression companion for BUG #3 — same widen to (OSError, RuntimeError).
    """
    project = str(tmp_path)
    with open(os.path.join(project, ".crabcakes"), "w", encoding="utf-8") as f:
        f.write("not a directory")

    with caplog.at_level(logging.ERROR, logger="utils.work_persistence"):
        write_tasks_summary(project, [WorkUnit(id="00000001", title="t")])  # no raise
    assert not os.path.exists(tasks_summary_path(project))


def test_save_with_dotcrabcakes_is_file(tmp_path, caplog):
    """Corrupt project state (.crabcakes is a regular file) must not crash
    save_work_units: _ensure_crabcakes_dir raises RuntimeError; log and
    return silently (spec §3.1 'never crash project open').

    Regression for BUG #3 (HIGH)."""
    project = str(tmp_path)
    with open(os.path.join(project, ".crabcakes"), "w", encoding="utf-8") as f:
        f.write("not a directory")

    with caplog.at_level(logging.ERROR, logger="utils.work_persistence"):
        save_work_units(project, [WorkUnit(id="00000001", title="t")])  # must not raise
    # Nothing could be written — .crabcakes is not a directory
    assert not os.path.exists(os.path.join(project, ".crabcakes", "work.json"))


# ── Legacy migration (spec §3.2) ─────────────────────────────────────────────

LEGACY_EXAMPLE = """## Task 00000003: File watcher core — 🔄 in_progress
- **Priority:** high
- **Assigned:** special:coder

## Task 00000004: API integration — 🚫 blocked
- **Priority:** medium
- **Notes:** waiting for credentials
"""


def _seed_tasks_md(project: str, content: str) -> None:
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab, exist_ok=True)
    with open(os.path.join(crab, "tasks.md"), "w", encoding="utf-8") as f:
        f.write(content)


def test_migrate_legacy_tasks_md(tmp_path):
    project = str(tmp_path)
    _seed_tasks_md(project, LEGACY_EXAMPLE)

    migrated = load_or_migrate_work_units(project)

    assert len(migrated) == 2
    by_id = {w.id: w for w in migrated}
    assert by_id["00000003"].title == "File watcher core"
    assert by_id["00000003"].status == "in-progress"
    assert by_id["00000003"].priority == "high"
    assert by_id["00000003"].spec_path == ""
    assert by_id["00000003"].blocked_reason == ""
    assert by_id["00000004"].title == "API integration"
    assert by_id["00000004"].status == "in-progress"
    assert by_id["00000004"].priority == "medium"
    assert by_id["00000004"].spec_path == ""
    assert by_id["00000004"].blocked_reason == "waiting for credentials"

    # work.json written
    assert os.path.isfile(os.path.join(project, ".crabcakes", "work.json"))
    # tasks.md regenerated (now carries the source-of-truth note)
    with open(os.path.join(project, ".crabcakes", "tasks.md"), "r", encoding="utf-8") as f:
        regenerated = f.read()
    assert SOURCE_OF_TRUTH_NOTE in regenerated


def test_migrate_legacy_status_mapping_all(tmp_path):
    content = (
        "## Task 00000001: p — 📝 pending\n"
        "## Task 00000002: i — 🔄 in_progress\n"
        "## Task 00000003: b — 🚫 blocked\n"
        "- **Notes:** stuck on creds\n"
        "## Task 00000004: d — ✅ done\n"
        "## Task 00000005: c — ❌ cancelled\n"
    )
    project = str(tmp_path)
    _seed_tasks_md(project, content)

    migrated = load_or_migrate_work_units(project)
    by_id = {w.id: w for w in migrated}

    assert by_id["00000001"].status == "draft"        # pending -> draft (no spec)
    assert by_id["00000002"].status == "in-progress"
    assert by_id["00000003"].status == "in-progress"  # blocked -> in-progress
    assert by_id["00000003"].blocked_reason == "stuck on creds"
    assert by_id["00000004"].status == "done"
    assert by_id["00000005"].status == "cancelled"


def test_migration_idempotent(tmp_path):
    project = str(tmp_path)
    _seed_tasks_md(project, LEGACY_EXAMPLE)

    first = load_or_migrate_work_units(project)
    second = load_or_migrate_work_units(project)

    assert len(first) == 2
    assert len(second) == 2  # no duplicates from re-migration
    assert [w.id for w in second] == [w.id for w in first]


def test_migration_no_recognizable_tasks_writes_nothing(tmp_path):
    project = str(tmp_path)
    prose = "# Random notes\n\nSome prose that is not a task.\n- **Priority:** high\n"
    _seed_tasks_md(project, prose)

    assert load_or_migrate_work_units(project) == []
    # No work.json may be written
    assert not os.path.exists(os.path.join(project, ".crabcakes", "work.json"))
    # Original tasks.md untouched
    with open(os.path.join(project, ".crabcakes", "tasks.md"), "r", encoding="utf-8") as f:
        assert f.read() == prose


def test_migration_heading_with_unparseable_body_no_crash(tmp_path):
    project = str(tmp_path)
    _seed_tasks_md(project, (
        "## Task 00000007: Weird — 🌀 mysterious_status\n"
        "this body is garbage\n"
        "| not a bullet |\n"
        "just prose\n"
    ))

    migrated = load_or_migrate_work_units(project)  # must not raise
    assert any(w.id == "00000007" for w in migrated)
    assert migrated[0].status == "draft"             # unrecognized -> draft default
    assert migrated[0].title == "Weird"


def test_migration_canceled_us_spelling(tmp_path):
    """US spelling 'canceled' (single l) in the legacy status label must map
    to the canonical legacy status 'cancelled' -> Work Unit status 'cancelled'.

    Regression for the BUG #6 test gap — the alias existed in
    _LEGACY_STATUS_ALIASES but had no covering test."""
    project = str(tmp_path)
    _seed_tasks_md(
        project,
        "## Task 00000011: US spelling — ❌ canceled\n",
    )

    migrated = load_or_migrate_work_units(project)

    assert len(migrated) == 1
    assert migrated[0].id == "00000011"
    assert migrated[0].status == "cancelled"
    assert migrated[0].title == "US spelling"


def test_load_or_migrate_binary_tasks_md_no_crash(tmp_path, caplog):
    """Binary/non-UTF8 tasks.md must not crash the legacy migration path:
    the text-mode open raises UnicodeDecodeError, which escaped the
    OSError-only handler and aborted project open.

    Regression for BUG #1 (HIGH). errors='replace' lets the garbage decode
    to U+FFFD replacement chars that the heading regex never matches, so
    the result is [] with nothing written.
    """
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    with open(os.path.join(crab, "tasks.md"), "wb") as f:
        f.write(b"\x80\x81\x82\xff\xfe")

    with caplog.at_level(logging.WARNING, logger="utils.work_persistence"):
        assert load_or_migrate_work_units(project) == []  # must not raise
    # Nothing recognizable -> nothing written (Step 3 contract)
    assert not os.path.exists(os.path.join(crab, "work.json"))
    assert os.path.isfile(os.path.join(crab, "tasks.md"))


def test_load_or_migrate_summary_valueerror_no_crash(tmp_path, monkeypatch, caplog):
    """A non-OSError (ValueError/TypeError) escaping write_tasks_summary in
    the MIGRATION path must not crash project open — the JSON was already
    durably written.

    Regression for BUG #2 (HIGH): the migration write_tasks_summary call
    had no try/except, so the raise escaped even though work.json existed.
    """
    project = str(tmp_path)
    _seed_tasks_md(project, "## Task 00000003: durable — 🔄 in_progress\n")

    def boom(*args, **kwargs):
        raise ValueError("summary render exploded in migration")

    monkeypatch.setattr("utils.work_persistence.write_tasks_summary", boom)

    with caplog.at_level(logging.ERROR, logger="utils.work_persistence"):
        migrated = load_or_migrate_work_units(project)  # must not raise

    assert [w.id for w in migrated] == ["00000003"]
    # work.json durably written BEFORE the summary — must survive
    assert os.path.isfile(os.path.join(project, ".crabcakes", "work.json"))
    with open(os.path.join(project, ".crabcakes", "work.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["work_units"][0]["id"] == "00000003"


def test_migration_valid_json_wins_over_stale_tasks_md(tmp_path):
    project = str(tmp_path)
    _seed_tasks_md(project, LEGACY_EXAMPLE)
    crab = os.path.join(project, ".crabcakes")
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "work_units": [
            {"id": "00000042", "title": "json wins", "status": "done"},
        ]}, f)

    result = load_or_migrate_work_units(project)

    assert [w.id for w in result] == ["00000042"]  # from JSON, not legacy md
    # tasks.md regenerated FROM the JSON (source-of-truth note present)
    with open(os.path.join(crab, "tasks.md"), "r", encoding="utf-8") as f:
        summary = f.read()
    assert SOURCE_OF_TRUTH_NOTE in summary
    assert "json wins" in summary
    assert "File watcher core" not in summary


def test_migration_invalid_json_does_not_migrate(tmp_path, caplog):
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        f.write("{broken")
    _seed_tasks_md(project, LEGACY_EXAMPLE)

    with caplog.at_level(logging.WARNING, logger="utils.work_persistence"):
        result = load_or_migrate_work_units(project)

    # Invalid work.json must NOT trigger legacy migration over it; the
    # existing file always wins (spec §8) and the load is a safe empty.
    assert result == []
    # Original legacy tasks.md untouched (never overwritten)
    with open(os.path.join(crab, "tasks.md"), "r", encoding="utf-8") as f:
        assert f.read() == LEGACY_EXAMPLE


# ── Work leases (SPEC-09 SP1) ────────────────────────────────────────────────

_LEASE_HOLDER_A = "special:coder"
_LEASE_HOLDER_B = "special:debugger"


def _seed_lease_store(project: str, unit_id: str = "00000001") -> WorkUnit:
    """Persist one in-progress unit for lease tests; returns the unit."""
    unit = WorkUnit(id=unit_id, title="lease target", status="in-progress")
    save_work_units(project, [unit])
    return unit


def test_claim_release_roundtrip(tmp_path):
    """Claim -> assert True -> release -> assert False (spec AC)."""
    project = str(tmp_path)
    _seed_lease_store(project)

    lease = claim_work(project, "00000001", _LEASE_HOLDER_A)
    assert lease is not None
    assert lease.unit_id == "00000001"
    assert lease.holder == _LEASE_HOLDER_A
    assert lease.ttl_seconds == DEFAULT_LEASE_TTL_SECONDS
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True

    assert release_work(project, "00000001", _LEASE_HOLDER_A) is True
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is False


def test_double_claim_refused(tmp_path):
    """A claims; B's claim returns None while A's lease is live (spec AC)."""
    project = str(tmp_path)
    _seed_lease_store(project)
    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is not None

    assert claim_work(project, "00000001", _LEASE_HOLDER_B) is None
    # A still holds it
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True


def test_same_holder_reclaim_refreshes(tmp_path, monkeypatch):
    """A re-claims while live: heartbeat — claimed_at advances, new lease."""
    project = str(tmp_path)
    _seed_lease_store(project)

    t = [1000.0]

    def fake_now():
        t[0] += 30.0  # every clock read advances 30s
        return t[0]

    monkeypatch.setattr("utils.work_persistence._now", fake_now)

    first = claim_work(project, "00000001", _LEASE_HOLDER_A)
    assert first is not None
    second = claim_work(project, "00000001", _LEASE_HOLDER_A)
    assert second is not None
    assert second.claimed_at > first.claimed_at
    assert second.holder == first.holder == _LEASE_HOLDER_A
    # refreshed claim still live for the same holder
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True


def test_ttl_expiry_reclaimable(tmp_path, monkeypatch):
    """Tiny ttl; time advances; B re-claims successfully; A's assert False."""
    project = str(tmp_path)
    _seed_lease_store(project)

    clock = {"now": 1000.0}

    def fake_now():
        return clock["now"]

    monkeypatch.setattr("utils.work_persistence._now", fake_now)

    assert claim_work(project, "00000001", _LEASE_HOLDER_A, ttl=60) is not None
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True

    clock["now"] += 61.0  # past the 60s ttl
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is False
    # B may now claim what A let expire (spec AC)
    lease_b = claim_work(project, "00000001", _LEASE_HOLDER_B)
    assert lease_b is not None
    assert lease_b.holder == _LEASE_HOLDER_B
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is False


def test_release_wrong_holder_refused(tmp_path):
    """A claims; B releases -> False; A still holds (spec AC)."""
    project = str(tmp_path)
    _seed_lease_store(project)
    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is not None

    assert release_work(project, "00000001", _LEASE_HOLDER_B) is False
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True


def test_expired_release_by_anyone(tmp_path, monkeypatch):
    """An expired lease is nobody's: B may release it (True, cleared)."""
    project = str(tmp_path)
    _seed_lease_store(project)

    clock = {"now": 1000.0}
    monkeypatch.setattr("utils.work_persistence._now", lambda: clock["now"])

    assert claim_work(project, "00000001", _LEASE_HOLDER_A, ttl=60) is not None

    clock["now"] += 61.0
    assert release_work(project, "00000001", _LEASE_HOLDER_B) is True
    # cleared on disk — A cannot assert it back into existence
    loaded = load_work_units(project)
    assert loaded[0].lease is None
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is False


def test_unknown_unit_never_raises(tmp_path):
    """claim/release/assert on a never-persisted id -> None/False/False."""
    project = str(tmp_path)
    _seed_lease_store(project, "00000001")

    assert claim_work(project, "99999999", _LEASE_HOLDER_A) is None
    assert release_work(project, "99999999", _LEASE_HOLDER_A) is False
    assert assert_lease(project, "99999999", _LEASE_HOLDER_A) is False


def test_ttl_zero_raises(tmp_path):
    """ttl <= 0 raises ValueError at claim, BEFORE any store interaction
    (programmer error, not runtime state). Negative ttl likewise."""
    _seed_lease_store(str(tmp_path))
    with pytest.raises(ValueError):
        claim_work(str(tmp_path), "00000001", _LEASE_HOLDER_A, ttl=0)
    with pytest.raises(ValueError):
        claim_work(str(tmp_path), "00000001", _LEASE_HOLDER_A, ttl=-1.0)


def test_lease_persists_across_store_reload(tmp_path):
    """The lease is durable, not in-memory: a fresh load of the SAME file
    sees A's live claim (D3 — lease lives in the unit record)."""
    project = str(tmp_path)
    _seed_lease_store(project)

    lease = claim_work(project, "00000001", _LEASE_HOLDER_A)
    assert lease is not None

    # Fresh load from disk — no shared in-memory state involved
    loaded = load_work_units(project)
    assert len(loaded) == 1
    disk_lease = loaded[0].lease
    assert disk_lease is not None
    assert disk_lease.holder == _LEASE_HOLDER_A
    assert disk_lease.unit_id == "00000001"
    assert disk_lease.claimed_at == pytest.approx(lease.claimed_at)
    assert disk_lease.ttl_seconds == DEFAULT_LEASE_TTL_SECONDS
    # and the API agrees through the fresh path
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True


def test_lease_survives_unit_field_edit(tmp_path):
    """D3's real promise: a lease survives an UNRELATED unit-field edit made
    through the legacy save path — the lease rides the record it lives in."""
    project = str(tmp_path)
    _seed_lease_store(project)
    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is not None

    # Load (lease present), edit an unrelated field, save via the legacy path
    units = load_work_units(project)
    units[0].title = "renamed by the PM"
    save_work_units(project, units)

    loaded = load_work_units(project)
    assert loaded[0].title == "renamed by the PM"
    assert loaded[0].lease is not None
    assert loaded[0].lease.holder == _LEASE_HOLDER_A
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True


def test_atomicity_under_concurrent_claims(tmp_path, monkeypatch):
    """BUG#4 rebuild: 4 contenders x 50 trials — exactly-one-winner EVERY
    trial, no thread crashes, file parses + matches the winner. Plus a
    lock-removal SENSITIVITY meta-check in the same test proving the pin has
    power: with _LEASE_LOCK replaced by a no-op, the auditor's no-lock data
    (155/200 double-winner, 17/200 torn writes) predicts the race surfaces
    within a few trials. If the no-op run passes 10/10 trials cleanly, SKIP
    (power is scheduling-dependent) — never fail-flake on scheduler luck.

    Runs the sensitivity FIRST; monkeypatch restores the real lock after.
    """
    project = str(tmp_path)
    _seed_lease_store(project)

    class _NoLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    # ── sensitivity: the pin must be ABLE to fail ────────────────────────
    monkeypatch.setattr("utils.work_persistence._LEASE_LOCK", _NoLock())
    detected = any(
        not _one_claim_trial(project) for _trial in range(10)
    )
    if not detected:
        pytest.skip(
            "no-lock configuration passed 10/10 trials cleanly — pin power "
            "not demonstrated this run (scheduling-dependent); skipping the "
            "sensitivity claim rather than fail-flaking"
        )

    # ── the pin: real lock (monkeypatch restored), 50 clean trials ──────
    for trial in range(50):
        assert _one_claim_trial(project), f"trial {trial}: lease race leaked"


def _one_claim_trial(project: str) -> bool:
    """One 4-thread barrier-start claim trial on unit 00000001.

    True iff ALL FOUR contenders returned (a crash counts as a violation —
    under the real lock a crashed claim means the pin caught something real),
    exactly one returned a lease, the others None, and the persisted file
    parses and carries that winner.
    """
    barrier = threading.Barrier(4)
    results: list = []
    results_lock = threading.Lock()

    def contender(holder: str):
        barrier.wait()
        try:
            outcome = claim_work(project, "00000001", holder)
        except Exception:  # noqa: BLE001 — a crashed contender IS a violation
            outcome = "__CRASHED__"
        with results_lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=contender, args=(f"holder-{i}",))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if len(results) != 4 or "__CRASHED__" in results:
        return False
    winners = [r for r in results if r is not None]
    if len(winners) != 1:
        return False
    with open(work_json_path(project), "r", encoding="utf-8") as f:
        data = json.load(f)
    lease = data["work_units"][0]["lease"]
    return lease is not None and lease["holder"] == winners[0].holder


def test_claim_survives_handler_mutation(tmp_path):
    """Probe-D integration pin through the REAL WorkHandler: a live lease
    survives a /work priority mutation whose store snapshot was loaded
    BEFORE the claim (stale-None in-memory lease must not erase it)."""
    from models.command import Command
    from ui.handlers.work_handler import WorkHandler

    class FakeProjectHandler:
        def __init__(self, path):
            self._path = path

        def get_active_project_name(self):
            return "proj"

        def get_active_project_path(self):
            return self._path

        def get_project_members(self, name):
            return []

    project = str(tmp_path)
    _seed_lease_store(project)
    store = WorkUnitStore()
    handler = WorkHandler(FakeProjectHandler(project), store)
    handler.load_for_project(project)  # snapshot BEFORE the claim: lease=None

    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is not None

    result = handler.cmd_work_priority(
        Command(name="work", args=["priority", "#00000001", "high"],
                source_session_key="project:proj")
    )
    assert result.handled and result.response_text  # mutation succeeded

    fresh = load_work_units(project)
    assert fresh[0].priority == "high"              # mutation persisted
    assert fresh[0].lease is not None               # lease INTACT
    assert fresh[0].lease.holder == _LEASE_HOLDER_A
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True
    assert claim_work(project, "00000001", _LEASE_HOLDER_B) is None


def test_claim_survives_done_mutation(tmp_path):
    """Done-variant of the probe-D pin: a live lease survives /work done
    (lease and status are orthogonal; SP3 wiring decides release-on-done
    policy — the storage layer only guarantees the lease is not CLOBBERED)."""
    import os as _os

    from models.command import Command
    from ui.handlers.work_handler import WorkHandler

    class FakeProjectHandler:
        def __init__(self, path):
            self._path = path

        def get_active_project_name(self):
            return "proj"

        def get_active_project_path(self):
            return self._path

        def get_project_members(self, name):
            return []

    project = str(tmp_path)
    spec_rel = "docs/specs/SPEC-lease.md"
    spec_full = _os.path.join(project, spec_rel)
    _os.makedirs(_os.path.dirname(spec_full))
    with open(spec_full, "w", encoding="utf-8") as f:
        f.write("# spec\n")
    unit = WorkUnit(
        id="00000001", title="lease target",
        status="in-progress", spec_path=spec_rel,
    )
    save_work_units(project, [unit])

    store = WorkUnitStore()
    handler = WorkHandler(FakeProjectHandler(project), store)
    handler.load_for_project(project)  # snapshot BEFORE the claim

    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is not None
    result = handler.cmd_work_done(
        Command(name="work", args=["done", "#00000001"],
                source_session_key="project:proj")
    )
    assert result.response_text is not None
    assert "done" in result.response_text.lower()

    loaded = load_work_units(project)
    assert loaded[0].status == "done"           # mutation persisted
    assert loaded[0].lease is not None          # lease NOT clobbered
    assert loaded[0].lease.holder == _LEASE_HOLDER_A
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is True


def test_released_lease_not_resurrected(tmp_path):
    """Probe G-A: store snapshot loaded AFTER the claim carries a stale
    non-None lease; a later release must stay released through handler
    mutations (the merge must not key on 'written is None')."""
    from models.command import Command
    from ui.handlers.work_handler import WorkHandler

    class FakeProjectHandler:
        def __init__(self, path):
            self._path = path

        def get_active_project_name(self):
            return "proj"

        def get_active_project_path(self):
            return self._path

        def get_project_members(self, name):
            return []

    project = str(tmp_path)
    _seed_lease_store(project)
    store = WorkUnitStore()
    handler = WorkHandler(FakeProjectHandler(project), store)

    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is not None
    handler.load_for_project(project)  # snapshot: lease=coder (stale soon)

    assert release_work(project, "00000001", _LEASE_HOLDER_A) is True
    assert load_work_units(project)[0].lease is None

    handler.cmd_work_priority(
        Command(name="work", args=["priority", "#00000001", "low"],
                source_session_key="project:proj")
    )
    # release NOT resurrected by the merge
    assert load_work_units(project)[0].lease is None
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is False


def test_stale_lease_does_not_overwrite_new_holder(tmp_path, monkeypatch):
    """Probe G-B: A claims; the store loads A's lease; it EXPIRES; B claims.
    A handler mutation must NOT overwrite B's live lease with A's stale
    in-memory copy. Deterministic via the frozen lease clock (a 1ms real-time
    ttl races the adjacent load_for_project — first draft flaked 1/5)."""
    from models.command import Command
    from ui.handlers.work_handler import WorkHandler

    class FakeProjectHandler:
        def __init__(self, path):
            self._path = path

        def get_active_project_name(self):
            return "proj"

        def get_active_project_path(self):
            return self._path

        def get_project_members(self, name):
            return []

    project = str(tmp_path)
    _seed_lease_store(project)
    store = WorkUnitStore()
    handler = WorkHandler(FakeProjectHandler(project), store)

    clock = {"now": 1000.0}
    monkeypatch.setattr("utils.work_persistence._now", lambda: clock["now"])

    assert claim_work(project, "00000001", "holder-A", ttl=60) is not None
    handler.load_for_project(project)           # snapshot: lease=A (live)

    clock["now"] += 61.0                        # A's lease now expired
    lease_b = claim_work(project, "00000001", "holder-B")
    assert lease_b is not None                  # A expired; B claimed

    handler.cmd_work_priority(
        Command(name="work", args=["priority", "#00000001", "medium"],
                source_session_key="project:proj")
    )
    disk = load_work_units(project)[0].lease
    assert disk is not None
    assert disk.holder == "holder-B"            # B's LIVE lease survived
    assert assert_lease(project, "00000001", "holder-B") is True
    assert assert_lease(project, "00000001", "holder-A") is False


def test_expired_lease_not_preserved(tmp_path, monkeypatch):
    """The merge preserves LIVE on-disk leases only: an EXPIRED on-disk lease
    is dropped by a handler mutation (with preserve_leases=True)."""
    from models.command import Command
    from ui.handlers.work_handler import WorkHandler

    class FakeProjectHandler:
        def __init__(self, path):
            self._path = path

        def get_active_project_name(self):
            return "proj"

        def get_active_project_path(self):
            return self._path

        def get_project_members(self, name):
            return []

    project = str(tmp_path)
    _seed_lease_store(project)
    store = WorkUnitStore()
    handler = WorkHandler(FakeProjectHandler(project), store)
    handler.load_for_project(project)

    clock = {"now": 1000.0}
    monkeypatch.setattr("utils.work_persistence._now", lambda: clock["now"])

    assert claim_work(project, "00000001", _LEASE_HOLDER_A, ttl=60) is not None
    clock["now"] += 61.0                        # A's lease expired on disk

    handler.cmd_work_priority(
        Command(name="work", args=["priority", "#00000001", "high"],
                source_session_key="project:proj")
    )
    assert load_work_units(project)[0].lease is None  # dropped, not preserved
    # ...and the unit is re-claimable by anyone now
    clock["now"] += 1.0
    assert claim_work(project, "00000001", _LEASE_HOLDER_B) is not None


def test_release_survives_handler_mutation(tmp_path):
    """The brief's original release pin, kept: pre-release-load order."""
    from models.command import Command
    from ui.handlers.work_handler import WorkHandler

    class FakeProjectHandler:
        def __init__(self, path):
            self._path = path

        def get_active_project_name(self):
            return "proj"

        def get_active_project_path(self):
            return self._path

        def get_project_members(self, name):
            return []

    project = str(tmp_path)
    _seed_lease_store(project)
    store = WorkUnitStore()
    handler = WorkHandler(FakeProjectHandler(project), store)
    handler.load_for_project(project)

    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is not None
    assert release_work(project, "00000001", _LEASE_HOLDER_A) is True

    handler.cmd_work_priority(
        Command(name="work", args=["priority", "#00000001", "high"],
                source_session_key="project:proj")
    )
    assert load_work_units(project)[0].lease is None


def test_save_units_returns_false_on_dir_failure(tmp_path, monkeypatch):
    """BUG#2: the silent no-op path returns False (was None) — no phantom
    success; existing callers may ignore it."""
    project = str(tmp_path)

    def boom(path):
        raise RuntimeError(".crabcakes is a regular file")

    monkeypatch.setattr("utils.work_persistence._ensure_crabcakes_dir", boom)
    ok = save_work_units(project, [WorkUnit(id="00000001", title="x")])
    assert ok is False


def test_claim_release_honor_persist_bool(tmp_path, monkeypatch):
    """Probe E: a silent no-op persist must NOT report success — claim
    returns None, release returns False, and nothing claims it landed."""
    project = str(tmp_path)
    _seed_lease_store(project)

    def boom(path):
        raise RuntimeError(".crabcakes is a regular file")

    monkeypatch.setattr("utils.work_persistence._ensure_crabcakes_dir", boom)

    assert claim_work(project, "00000001", _LEASE_HOLDER_A) is None
    assert release_work(project, "00000001", _LEASE_HOLDER_A) is False
    assert assert_lease(project, "00000001", _LEASE_HOLDER_A) is False


def test_corrupt_lease_keeps_unit_and_heals(tmp_path, caplog):
    """Probe A: a malformed lease must not discard the whole unit — the
    record loads with lease=None, warns, and a re-save heals the record."""
    project = str(tmp_path)
    crab = os.path.join(project, ".crabcakes")
    os.makedirs(crab)
    payload = {
        "version": 1,
        "work_units": [
            {"id": "00000001", "title": "victim", "status": "in-progress",
             "lease": {"unit_id": "00000001", "holder": 12345,
                       "claimed_at": 1.0, "ttl_seconds": 60.0}},
            {"id": "00000002", "title": "innocent", "status": "draft"},
        ],
    }
    with open(os.path.join(crab, "work.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)

    with caplog.at_level(logging.WARNING, logger="models.work_unit"):
        loaded = load_work_units(project)

    assert [w.id for w in loaded] == ["00000001", "00000002"]  # unit KEPT
    assert loaded[0].lease is None                              # lease dropped
    assert "dropping corrupt lease" in caplog.text              # warned

    # heal: re-save through the legacy path rewrites lease: null
    save_work_units(project, loaded)
    with open(work_json_path(project), "r", encoding="utf-8") as f:
        healed = json.load(f)
    assert healed["work_units"][0]["lease"] is None


def test_unit_id_normalization_probe_f(tmp_path):
    """Probe F: '#3', '3', and '00000003' all claim the SAME unit; a
    non-numeric id raises instead of silently missing."""
    project = str(tmp_path)
    _seed_lease_store(project, "00000003")

    lease = claim_work(project, "#3", _LEASE_HOLDER_A)
    assert lease is not None
    assert lease.unit_id == "00000003"
    assert assert_lease(project, "3", _LEASE_HOLDER_A) is True
    # different holder refused by canonical id too
    assert claim_work(project, "00000003", _LEASE_HOLDER_B) is None

    with pytest.raises(ValueError):
        claim_work(project, "not-numeric", _LEASE_HOLDER_A)
    with pytest.raises(ValueError):
        release_work(project, "", _LEASE_HOLDER_A)


def test_ttl_nan_and_inf_raise(tmp_path):
    """NaN/inf ttl would poison read-time expiry math (NaN < x is always
    False → unexpirable) — both refused with ValueError."""
    project = str(tmp_path)
    _seed_lease_store(project)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            claim_work(project, "00000001", _LEASE_HOLDER_A, ttl=bad)
