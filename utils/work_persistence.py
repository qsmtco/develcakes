# utils/work_persistence.py
# Work Unit persistence layer — .crabcakes/work.json is the source of truth;
# .crabcakes/tasks.md is a generated summary; legacy tasks.md migrates once.
#
# Spec: SPEC-TASK-SYSTEM-FULL-REDESIGN §3 (Persistence and Migration), Phase 2.
# Architecture: pure Python utility. May import models.work_unit and
# utils.project_awareness only. NO imports from ui/, gateway/, or agent/.

import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Iterable

from models.work_unit import (
    WORK_PRIORITIES,
    WORK_PRIORITY_LABELS,
    WORK_STATUS_LABELS,
    WorkLease,
    WorkUnit,
    _work_init_counter,
)
from utils.project_awareness import _ensure_crabcakes_dir, get_crabcakes_dir

_logger = logging.getLogger(__name__)


# ── Format constants ─────────────────────────────────────────────────────────

WORK_JSON_FILENAME = "work.json"
TASKS_SUMMARY_FILENAME = "tasks.md"
WORK_JSON_VERSION = 1

SOURCE_OF_TRUTH_NOTE = (
    "Generated from `.crabcakes/work.json`; "
    "edit work units through `/work` commands."
)

# Legacy tasks.md section headings (spec §3.2 example):
#   ## Task 00000003: File watcher core — 🔄 in_progress
_LEGACY_TASK_HEADING_RE = re.compile(r"^##\s+Task\s+(\d+)\s*:\s*(.+)$")
_LEGACY_PRIORITY_RE = re.compile(r"^-\s*\*\*Priority:\*\*\s*(.+?)\s*$")
_LEGACY_ASSIGNED_RE = re.compile(r"^-\s*\*\*Assigned:\*\*\s*(.+?)\s*$")
_LEGACY_NOTES_RE = re.compile(r"^-\s*\*\*Notes:\*\*\s*(.+?)\s*$")

# Legacy status label (after emoji strip) -> canonical legacy status.
_LEGACY_STATUS_ALIASES = {
    "pending": "pending",
    "in_progress": "in_progress",
    "in-progress": "in_progress",
    "in progress": "in_progress",
    "blocked": "blocked",
    "done": "done",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

# Canonical legacy status -> Work Unit status (spec §3.2).
_LEGACY_TO_WORK_STATUS = {
    "pending": "draft",        # NOT spec-ready — no spec exists
    "in_progress": "in-progress",
    "blocked": "in-progress",  # + blocked_reason from Notes
    "done": "done",
    "cancelled": "cancelled",
}


# ── Path helpers ─────────────────────────────────────────────────────────────


def work_json_path(project_path: str) -> str:
    """Return <project>/.crabcakes/work.json."""
    return os.path.join(get_crabcakes_dir(project_path), WORK_JSON_FILENAME)


def tasks_summary_path(project_path: str) -> str:
    """Return <project>/.crabcakes/tasks.md."""
    return os.path.join(get_crabcakes_dir(project_path), TASKS_SUMMARY_FILENAME)


# ── Atomic writes (repo convention: .tmp + os.replace) ───────────────────────


def _atomic_write_json(path: str, payload: dict) -> None:
    """Write JSON atomically via a temp file + os.replace (crash-safe)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _atomic_write_text(path: str, content: str) -> None:
    """Write text atomically via a temp file + os.replace (crash-safe)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


# ── Loading ──────────────────────────────────────────────────────────────────


def _load_valid_work_json(project_path: str) -> list[WorkUnit] | None:
    """Parse .crabcakes/work.json into Work Units.

    Returns the loaded list when the file parses into the versioned shape
    (possibly an empty list for a valid empty store), or None when the file
    is missing, invalid JSON, or has the wrong top-level shape. Invalid files
    are logged as warnings and never raised.
    """
    path = work_json_path(project_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _logger.warning("load_work_units: cannot read %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "load_work_units: %s top level is not an object — returning empty", path
        )
        return None
    if data.get("version") != WORK_JSON_VERSION:
        _logger.warning(
            "load_work_units: %s has unsupported version %r — returning empty",
            path,
            data.get("version"),
        )
        return None
    raw_units = data.get("work_units")
    if not isinstance(raw_units, list):
        _logger.warning(
            "load_work_units: %s 'work_units' is not a list — returning empty", path
        )
        return None

    loaded: list[WorkUnit] = []
    for index, record in enumerate(raw_units):
        try:
            loaded.append(WorkUnit.from_dict(record))
        except ValueError as e:
            # Best-effort: one bad record must not abort the whole load.
            _logger.warning(
                "load_work_units: skipping malformed record %d in %s: %s",
                index,
                path,
                e,
            )
            continue
    return loaded


def load_work_units(project_path: str) -> list[WorkUnit]:
    """Load persisted Work Units from .crabcakes/work.json.

    Missing file, invalid JSON, or a wrong top-level shape returns [] with a
    logged warning — never raises. Malformed records are skipped best-effort.

    After a successful load the module ID counter is advanced past the loaded
    IDs (via _work_init_counter) so new Work Units do not collide after a
    restart. The counter is NOT touched on the empty paths (missing/invalid/
    shape errors) — no IDs were loaded, so there is nothing to advance past.
    """
    loaded = _load_valid_work_json(project_path)
    if loaded is None:
        return []
    if loaded:
        _work_init_counter(loaded)
    return loaded


# ── Saving ───────────────────────────────────────────────────────────────────


def _merge_preserved_leases(
    units: list[WorkUnit], on_disk: dict[str, WorkUnit]
) -> None:
    """Carry on-disk lease state into the records being written (BUG#1/probe G).

    Rule: the DISK owns the lease. For each written unit that has an on-disk
    counterpart, the resulting lease is the ON-DISK lease when it is non-None
    AND still live; otherwise None. The in-memory lease is IGNORED entirely —
    a store snapshot loaded before a claim/release carries no lease intent,
    in either direction: stale-None would erase a live claim (probe D), while
    stale-non-None would resurrect a released lease (probe G-A) or overwrite a
    newer holder's live lease (probe G-B). New units (no on-disk record) keep
    their in-memory lease (normally None). The caller holds _LEASE_LOCK across
    both the disk read and the write; the disk read uses _load_valid_work_json
    (NOT load_work_units) so the module ID counter is not tripped.
    """
    now = _now()
    for unit in units:
        disk_unit = on_disk.get(unit.id)
        if disk_unit is None:
            continue  # new unit — no on-disk counterpart
        disk_lease = disk_unit.lease
        if disk_lease is not None and _lease_is_live(disk_lease, now):
            if unit.lease is None or unit.lease != disk_lease:
                _logger.debug(
                    "save_work_units: preserved live lease for %s "
                    "(stale in-memory snapshot)",
                    unit.id,
                )
            unit.lease = disk_lease
        else:
            if unit.lease is not None:
                _logger.debug(
                    "save_work_units: dropped stale in-memory lease for %s "
                    "(disk owns the lease; on-disk state absent/expired)",
                    unit.id,
                )
            unit.lease = None


def save_work_units(
    project_path: str,
    work_units: Iterable[WorkUnit],
    *,
    preserve_leases: bool = True,
) -> bool:
    """Persist Work Units to .crabcakes/work.json, then regenerate tasks.md.

    The JSON write is atomic (temp file + os.replace) and completes BEFORE the
    summary write. A failed summary write is logged and never corrupts or
    rolls back the JSON source of truth.

    Lease merge (SPEC-09 SP1 fix, BUG#1 + probe G): with the default
    ``preserve_leases=True`` the ON-DISK lease state wins for every unit that
    already exists on disk — a live on-disk lease is carried into the write
    and an absent/expired one is cleared — so stale in-memory snapshots can
    neither erase a live claim nor resurrect a released lease. Pass
    ``preserve_leases=False`` when the written lease IS the intent (the lease
    API's own mutations: claim writes its new lease, release writes None).

    Returns True when work.json was persisted; False on the silent no-op path
    (directory preparation failed). Existing callers may ignore the return.

    A corrupt project state (.crabcakes is a regular file) raises RuntimeError
    from _ensure_crabcakes_dir — that is caught, logged, and the save is a
    silent no-op (spec §3.1: never crash project open).
    """
    units = list(work_units)
    try:
        _ensure_crabcakes_dir(project_path)
    except (OSError, RuntimeError) as e:
        _logger.error(
            "save_work_units: cannot prepare .crabcakes for %s: %s",
            project_path,
            e,
        )
        return False
    with _LEASE_LOCK:  # merge read + write atomic under one acquisition
        if preserve_leases:
            on_disk = _load_units_for_lease(project_path)
            _merge_preserved_leases(units, {w.id: w for w in on_disk})
        payload = {
            "version": WORK_JSON_VERSION,
            "work_units": [w.to_dict() for w in units],
        }
        _atomic_write_json(work_json_path(project_path), payload)
    try:
        write_tasks_summary(project_path, units)
    except Exception as e:  # defensive: summary must never corrupt work.json
        _logger.error(
            "save_work_units: summary write failed at %s "
            "(work.json preserved): %s",
            tasks_summary_path(project_path),
            e,
        )
    return True


# ── Generated summary ────────────────────────────────────────────────────────


def render_tasks_summary(work_units: Iterable[WorkUnit]) -> str:
    """Render a deterministic, stable human-readable summary of every Work Unit.

    Includes ID, title, status, priority, spec indicator/path, and assignment
    fields. Sorted by created_at ascending then id — the same order as
    WorkUnitStore.list_all(). The summary is for humans only: NO
    implementation path may parse this generated output after it is written;
    .crabcakes/work.json is the source of truth.
    """
    units = sorted(work_units, key=lambda w: (w.created_at, w.id))
    parts = ["# Work Units", "", SOURCE_OF_TRUTH_NOTE]
    for w in units:
        parts.append("")
        parts.append(f"## {w.id} — {w.title}".rstrip())
        parts.append(
            f"- **Status:** {WORK_STATUS_LABELS.get(w.status, w.status)}"
        )
        parts.append(
            f"- **Priority:** {WORK_PRIORITY_LABELS.get(w.priority, w.priority)}"
        )
        if w.spec_path:
            parts.append(f"- **Spec:** ✓ {w.spec_path}")
        else:
            parts.append("- **Spec:** ⚠ no spec")
        parts.append(f"- **Supervisor:** {w.assigned_supervisor}")
        parts.append(f"- **Builder:** {w.assigned_builder}")
        parts.append(f"- **Auditor:** {w.assigned_auditor}")
        if w.blocked_reason:
            parts.append(f"- **Blocked reason:** {w.blocked_reason}")
    return "\n".join(parts) + "\n"


def write_tasks_summary(project_path: str, work_units: Iterable[WorkUnit]) -> None:
    """Render and write the generated summary to .crabcakes/tasks.md.

    Best-effort: creates .crabcakes/ if needed and logs (does not raise) on
    OSError or RuntimeError (e.g. corrupt project state where .crabcakes is
    a regular file). Never touches work.json.
    """
    content = render_tasks_summary(work_units)
    try:
        _ensure_crabcakes_dir(project_path)
        _atomic_write_text(tasks_summary_path(project_path), content)
    except (OSError, RuntimeError) as e:
        _logger.error(
            "write_tasks_summary: failed to write %s: %s",
            tasks_summary_path(project_path),
            e,
        )


# ── Legacy migration ─────────────────────────────────────────────────────────


def _split_title_status(rest: str) -> tuple[str, str]:
    """Split 'title — status' on the LAST em/en-dash or spaced-hyphen separator."""
    for sep in (" — ", " – ", " - "):
        parts = rest.rsplit(sep, 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return rest.strip(), ""


def _normalize_legacy_status(status_text: str) -> str:
    """Strip a leading emoji/space run from a legacy status label and canonicalize.

    Recognizes the spec §3.2 statuses by their text after the emoji.
    Returns the canonical legacy status or "" when unrecognized.
    """
    if not status_text:
        return ""
    cleaned = re.sub(r"^\W+", "", status_text, flags=re.UNICODE).strip().lower()
    return _LEGACY_STATUS_ALIASES.get(cleaned, "")


def _parse_legacy_tasks_markdown(content: str) -> list[WorkUnit]:
    """Best-effort parse of legacy .crabcakes/tasks.md into Work Units.

    Recognizes sections of the form (spec §3.2 example):

        ## Task 00000003: File watcher core — 🔄 in_progress
        - **Priority:** high
        - **Assigned:** special:coder
        - **Notes:** waiting for credentials

    Section that doesn't match the heading regex are skipped (defensive —
    never crash on arbitrary markdown). A matching heading with an
    unparseable body still yields a unit with defaults; markdown is never
    fabricated into completed work. Unrecognized statuses default to 'draft'.
    Legacy 'blocked' units get blocked_reason from Notes.
    """
    units: list[WorkUnit] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        tid, title, status_text = current["heading"]
        status = _normalize_legacy_status(status_text)
        work_status = _LEGACY_TO_WORK_STATUS.get(status, "draft")
        priority = current.get("priority", "medium").strip().lower()
        if priority not in WORK_PRIORITIES:
            priority = "medium"
        unit = WorkUnit(
            id=str(int(tid)).zfill(8),
            title=title,
            spec_path="",
            status=work_status,
            priority=priority,
            assigned_builder=current.get("assigned", "special:coder"),
        )
        if status == "blocked" and current.get("notes"):
            unit.blocked_reason = current["notes"]
        units.append(unit)
        current = None

    for line in content.splitlines():
        stripped = line.strip()
        m = _LEGACY_TASK_HEADING_RE.match(stripped)
        if m:
            flush()
            tid, rest = m.group(1), m.group(2)
            title, status_text = _split_title_status(rest)
            current = {"heading": (tid, title, status_text)}
            continue
        if current is not None:
            pm = _LEGACY_PRIORITY_RE.match(stripped)
            if pm:
                current["priority"] = pm.group(1)
                continue
            am = _LEGACY_ASSIGNED_RE.match(stripped)
            if am:
                current["assigned"] = am.group(1)
                continue
            nm = _LEGACY_NOTES_RE.match(stripped)
            if nm:
                current["notes"] = nm.group(1)
                continue
            # Unrecognized bullet/prose in a matching section — ignored.
    flush()
    return units


def load_or_migrate_work_units(project_path: str) -> list[WorkUnit]:
    """Load the authoritative work.json, or best-effort migrate a legacy
    .crabcakes/tasks.md exactly once (spec §3.2).

    1. A valid versioned work.json is authoritative: load it, regenerate
       tasks.md from it, return the units (an existing file always wins,
       even when empty — stale tasks.md is never parsed).
    2. An absent work.json (no file) → parse legacy tasks.md best-effort;
       recognizable sections are persisted to work.json and tasks.md is
       regenerated exactly once. A present-but-invalid work.json is logged
       and returns [] WITHOUT migration (existing work.json is not assumed
       migratable; the legacy source is used only when work.json is absent).
    3. No recognizable tasks → [] and nothing is written.
    """
    json_path = work_json_path(project_path)

    # Step 1: existing, valid versioned JSON wins.
    if _load_valid_work_json(project_path) is not None:
        loaded = load_work_units(project_path)
        try:
            write_tasks_summary(project_path, loaded)
        except Exception as e:  # defensive: summary must never break project open
            _logger.error(
                "load_or_migrate_work_units: summary regenerate failed at %s "
                "(work.json preserved): %s",
                tasks_summary_path(project_path),
                e,
            )
        return loaded

    # Present but invalid → warning already logged; never migrate over it.
    if os.path.isfile(json_path):
        return []

    # Step 2: work.json absent → best-effort legacy migration.
    summary_path = tasks_summary_path(project_path)
    if not os.path.isfile(summary_path):
        return []
    try:
        with open(summary_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        _logger.warning(
            "load_or_migrate_work_units: cannot read %s: %s", summary_path, e
        )
        return []

    migrated = _parse_legacy_tasks_markdown(content)
    if not migrated:
        return []  # Step 3: nothing recognizable — write nothing.

    # Step 4: one-shot persist. work.json FIRST (original tasks.md untouched
    # until the JSON is durably written), then regenerate the summary.
    try:
        _ensure_crabcakes_dir(project_path)
        with _LEASE_LOCK:  # shared .tmp file with lease writes — see lock note
            _atomic_write_json(
                json_path,
                {"version": WORK_JSON_VERSION, "work_units": [w.to_dict() for w in migrated]},
            )
    except (OSError, RuntimeError) as e:
        _logger.warning(
            "load_or_migrate_work_units: failed to persist migration to %s: %s",
            json_path,
            e,
        )
        return []
    try:
        write_tasks_summary(project_path, migrated)
    except Exception as e:  # defensive: summary must never break project open
        _logger.error(
            "load_or_migrate_work_units: summary write failed at %s "
            "(work.json preserved): %s",
            tasks_summary_path(project_path),
            e,
        )
    _work_init_counter(migrated)  # advance past migrated ids before next create
    return migrated


# ── Work leases (SPEC-09 SP1) ────────────────────────────────────────────────
#
# D3: the lease lives INSIDE the WorkUnit record — one atomic work.json write
# per mutation, no sidecar file. TTL expiry is computed at READ time
# (``now - claimed_at < ttl``); there is NO background sweeper — an expired
# lease is simply re-claimable/releasable by anyone.
#
# Serialization: the module IS the store here (WorkUnitStore is deliberately
# in-memory-only by architecture), so one module-level reentrant lock guards
# EVERY work.json write (lease mutations AND the legacy save path — they share
# the same .tmp file, so unsynchronized writers could publish torn JSON).
# RLock because claim/release hold it across their own save_work_units call.
# Reads stay lock-free: os.replace is atomic, so a reader sees the old or the
# new file, never a mix. Scope note: this serializes threads within the
# (single-process) app; cross-PROCESS lease arbitration is out of MVP scope.

DEFAULT_LEASE_TTL_SECONDS = 900.0  # SPEC-09 §7: default 15 minutes

_LEASE_LOCK = threading.RLock()


def _now() -> float:
    """Wall clock for lease TTL math. Module-level indirection so tests can
    freeze/advance time via monkeypatch (utils.work_persistence._now)."""
    return time.time()


def _lease_is_live(lease: WorkLease, now: float) -> bool:
    """True when the lease has not expired at read time ``now``.

    Read-time TTL: no sweeper; an expired lease is simply re-claimable.
    """
    return (now - lease.claimed_at) < lease.ttl_seconds


def _validate_holder(holder: str) -> str:
    """Reject a non-string or empty holder (programmer error, like ttl <= 0).

    An empty holder could never be re-asserted or released meaningfully, so
    it is refused at the boundary instead of creating an unownable lease.
    """
    if not isinstance(holder, str) or not holder:
        raise ValueError("holder must be a non-empty string")
    return holder


def _normalize_unit_id(unit_id: str) -> str:
    """Canonicalize a unit id to the zero-padded 8-digit form (#3/3 → 00000003).

    The lease API never silently misses on id FORM: '#3', '3', and '00000003'
    all canonicalize to '00000003' (probe F). A malformed id (non-numeric
    after '#' stripping, or empty) raises ValueError — a programmer error,
    same class as ttl <= 0. An id that is well-formed but not in the store
    remains the *unknown unit* case (None/False, never raises).
    """
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise ValueError("unit_id must be a non-empty string")
    digits = unit_id.strip().lstrip("#")
    if not digits.isdigit():
        raise ValueError(
            f"unit_id must be numeric (optionally #-prefixed), got {unit_id!r}"
        )
    return str(int(digits)).zfill(8)


def _validate_ttl(ttl: float) -> float:
    """Reject a non-numeric, bool, <= 0, NaN, or inf ttl (programmer error).

    NaN and inf would poison the read-time TTL math (NaN < x is False forever
    → an unexpirable lease; inf likewise) — refused at the boundary.
    """
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise ValueError("ttl must be a number")  # noqa: TRY004 — spec: ValueError
    ttl = float(ttl)
    if not math.isfinite(ttl):
        raise ValueError(f"ttl must be finite, got {ttl}")
    if ttl <= 0:
        raise ValueError(f"ttl must be positive, got {ttl}")
    return ttl


def _load_units_for_lease(project_path: str) -> list[WorkUnit]:
    """Load the unit list for a lease operation while holding _LEASE_LOCK.

    A missing OR corrupt work.json maps to [] — every lease function then
    treats the unit as unknown (None/False, never raises). Callers must only
    save when a mutation actually landed, so an empty/corrupt store is never
    rewritten as an empty store.
    """
    loaded = _load_valid_work_json(project_path)
    return loaded if loaded is not None else []


def claim_work(
    project_path: str,
    unit_id: str,
    holder: str,
    ttl: float = DEFAULT_LEASE_TTL_SECONDS,
) -> WorkLease | None:
    """Claim a Work Unit if unclaimed or the existing lease is expired.

    Returns the new WorkLease on success; None = refused (another holder
    holds a LIVE lease, or the unit is unknown — never raises on unknown).

    Same-holder re-claim while live REFRESHES claimed_at (heartbeat
    semantics) and returns the new lease — idempotent re-entry, not a
    refusal. ttl <= 0 (or non-numeric) raises ValueError. An OSError from
    the underlying write propagates — the claim did not land.
    """
    unit_id = _normalize_unit_id(unit_id)
    _validate_holder(holder)
    ttl = _validate_ttl(ttl)
    with _LEASE_LOCK:
        units = _load_units_for_lease(project_path)
        unit = next((w for w in units if w.id == unit_id), None)
        if unit is None:
            return None
        existing = unit.lease
        if (
            existing is not None
            and _lease_is_live(existing, _now())
            and existing.holder != holder
        ):
            return None  # live lease, different holder — refused
        lease = WorkLease(
            unit_id=unit.id,
            holder=holder,
            claimed_at=_now(),
            ttl_seconds=ttl,
        )
        unit.lease = lease
        # preserve_leases=False: the written lease IS the intent — the merge
        # must not replace it with the (older) on-disk state. BUG#2: honor the
        # bool — a silent no-op persist must not report success.
        if not save_work_units(project_path, units, preserve_leases=False):
            _logger.warning(
                "claim_work: claim on unit %s not persisted (save failed); "
                "reporting refusal",
                unit_id,
            )
            return None
        return lease


def release_work(project_path: str, unit_id: str, holder: str) -> bool:
    """Release a Work Unit lease iff ``holder`` matches or the lease expired.

    True = released (lease cleared and persisted); False = not the holder of
    a live lease / not held / unit unknown (never raises on unknown). An
    expired lease is nobody's — anyone may release it.
    """
    unit_id = _normalize_unit_id(unit_id)
    _validate_holder(holder)
    with _LEASE_LOCK:
        units = _load_units_for_lease(project_path)
        unit = next((w for w in units if w.id == unit_id), None)
        if unit is None or unit.lease is None:
            return False
        if unit.lease.holder != holder and _lease_is_live(unit.lease, _now()):
            return False  # live lease owned by someone else
        unit.lease = None
        # preserve_leases=False: the written lease (None) IS the intent — the
        # merge would resurrect the live on-disk lease being released. BUG#2:
        # a silent no-op persist must not report success.
        if not save_work_units(project_path, units, preserve_leases=False):
            _logger.warning(
                "release_work: release of unit %s not persisted (save failed); "
                "reporting not-released",
                unit_id,
            )
            return False
        return True


def assert_lease(project_path: str, unit_id: str, holder: str) -> bool:
    """True iff ``holder`` holds a LIVE (not expired) lease on the unit.

    Pure check — reads work.json but never writes. False on unknown unit,
    no lease, wrong holder, or expiry (never raises on unknown).
    """
    unit_id = _normalize_unit_id(unit_id)
    _validate_holder(holder)
    with _LEASE_LOCK:
        units = _load_units_for_lease(project_path)
        unit = next((w for w in units if w.id == unit_id), None)
        if unit is None or unit.lease is None:
            return False
        return unit.lease.holder == holder and _lease_is_live(unit.lease, _now())


def find_live_lease(project_path: str, holder: str) -> WorkLease | None:
    """The holder's LIVE lease, or None (SPEC-09 SP2's ARH lookup).

    Holder-scoped SP1 read: scans work.json for a unit whose lease is held by
    ``holder`` AND still live (read-time TTL via _lease_is_live — the ONE
    liveness definition). Composes assert_lease's semantics for the
    'which unit does this session hold?' question assert_lease cannot answer
    (it needs unit_id). Under _LEASE_LOCK; never raises; never writes; does
    NOT trip the module ID counter (uses _load_units_for_lease's raw loader).
    """
    _validate_holder(holder)
    with _LEASE_LOCK:
        for unit in _load_units_for_lease(project_path):
            lease = unit.lease
            if (
                lease is not None
                and lease.holder == holder
                and _lease_is_live(lease, _now())
            ):
                return lease
        return None
