# SPEC-11 SP1 — migrate_v1_config(): one-time copy-with-verify migration

**Spec:** `docs/specs/SPEC-11-RENAME-MIGRATION.md` §2 (migration helper)
**Pre-flight (binding):** `docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md` — D3 (full
contract), D4 (config dir), BLOCKING-1 (state-dir rename deferred — NOT this phase)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `utils/config.py` (add `migrate_v1_config` + the new config-dir name) +
`tests/test_config_migration.py` (new). **No env-var changes yet** (SP2). **No main.py**
(SP3 wires the call).

---

## 1. `utils/config.py`

### 1a. Config dir divergence (D4)

`get_config_dir()` returns `develcakes` instead of `crabcakes`:

```python
def get_config_dir() -> str:
    """Return the develcakes config directory.

    Respects $XDG_CONFIG_HOME if set, otherwise ~/.config/develcakes.
    Does NOT create the directory.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "develcakes")
    return os.path.join(os.path.expanduser("~"), ".config", "develcakes")
```

Add a sibling the migration needs:

```python
def get_v1_config_dir() -> str:
    """The v1 (crabcakes) config dir — migration SOURCE only, never written."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "crabcakes")
    return os.path.join(os.path.expanduser("~"), ".config", "crabcakes")

MIGRATION_MARKER = "MIGRATED_FROM_V1"
```

### 1b. `migrate_v1_config()` (D3 contract)

```python
def migrate_v1_config() -> dict | None:
    """One-time copy-with-verify from the v1 config dir to the develcakes one.

    D3 contract:
    - Copy list: agent.json, providers.yaml, config.json, conversations/,
      agents/, audit-log.jsonl, projects/, transcript.db (+ -wal/-shm
      sidecars if present), feed-prefs.json if present.
    - No-op guards (return None, no banner): marker file exists OR the new
      dir already contains ANY copy-list entry (both-dirs case: new wins).
    - Verify: per-file byte counts post-copy; directories recursed.
    - Failure (any mismatch / IO error): delete the PARTIAL new-dir contents
      (only entries this migration created — never the marker, never
      pre-existing files), report {"failed": [...]}, app continues fresh.
      The v1 dir is NEVER touched (non-destructive, always).
    - Success: write the marker; return {"copied": [...], "skipped": [...],
      "failed": []} (skipped = copy-list entries absent from v1).
    """
```

Implementation rules (auditable):
- Pure `os`/`shutil` — no git, no GTK, no agent imports (utils layer).
- `transcript.db` and sidecars: copy + byte-count ONLY — never open the DB
  (a live WAL mid-checkpoint is fine for a file copy; opening it is not).
- Rollback deletes ONLY what the migration itself wrote (track the list).
- The marker is written ONLY on full success.
- The v1 dir is never written, never deleted, never renamed.
- Everything can raise internally but the function NEVER raises to its
  caller — failures return the report dict with `"failed"` populated
  (the caller decides banner text; this is utils, no UI).
- Docstring: the full D3 contract above, verbatim contract points.

### 1c. Callers — none yet

`migrate_v1_config` is defined but NOT called anywhere this phase (SP3 wires
main.py). Zero behavior change to the running app: `get_config_dir()`'s new
name only affects fresh reads — note in the diff report which existing
callers/tests would now point at the new path and confirm the suite stays
green (tests that patch `get_config_dir` are unaffected; tests that build on
`$XDG_CONFIG_HOME` fake homes get the new name — verify no test asserts the
old literal; if some do, they are spec-drift and get updated with disclosure).

## 2. Tests — `tests/test_config_migration.py` (new, RED-first)

Patterns: fake `$XDG_CONFIG_HOME` via monkeypatch (the file's tests/conftest
already models this — read it first), real tmp dirs, no mocks of os/shutil
(this is filesystem logic; mock nothing).

1. `test_migration_copies_all_entries` — v1 dir with the full copy list →
   migrate → every entry present in new dir, byte counts equal, marker
   written, report copied == full list.
2. `test_migration_marker_prevents_rerun` — second call returns None; new
   dir unchanged (mtime or content hash compare).
3. `test_migration_non_destructive_to_v1` — v1 files' content hashes
   unchanged post-migration.
4. `test_migration_fresh_install_no_v1` — no v1 dir → returns None, no new
   dir created.
5. `test_migration_both_dirs_content_new_wins` — new dir has config.json
   already → returns None, v1 untouched, new content unchanged.
6. `test_migration_partial_failure_rolls_back` — inject a failure (e.g. a
   destination file that can't be written: read-only parent trick or a
   pre-created dir where a copy-list entry is a DIRECTORY named like a
   file) → failed report; new dir has NONE of the migration's entries
   (rollback ran); v1 untouched; NO marker.
7. `test_migration_skips_absent_entries` — v1 missing some entries →
   skipped lists them, copied lists the rest, marker written.
8. `test_migration_transcript_sidecars` — transcript.db + -wal + -shm →
   all three copied, counts verified.
9. `test_migration_does_not_create_config_dir_on_noop` — marker-exists path
   with new dir absent → None, new dir still absent.

## 3. Battery (paste all)

- `python -m pytest tests/test_config_migration.py -q` (all new, RED-first)
- `python -m pytest tests/ -q -k "config"` (adjacent suites — conftest
  fakes, settings, provider config)
- `ruff check utils/config.py tests/test_config_migration.py`
- `pyright utils/config.py` vs baseline (measure first)
- `wc -l` both files
- RED proofs for the 9 tests

## 4. COMPLETENESS (mandatory)

- [ ] Edit 1: get_config_dir divergence + get_v1_config_dir + marker — hunks
- [ ] Edit 2: migrate_v1_config per D3 — hunk + contract docstring
- [ ] Tests 1–9 RED-first — outputs + count
- [ ] Adjacent-suite battery (config-keyed) — output
- [ ] ruff/pyright/wc — outputs
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
