# SPEC-11 SP1 Fix Round 2 — symlinked subdirs followed (cycle-guarded), file-name carve-out, verify test

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `utils/config.py` + `tests/test_config_migration.py`.
Binding ruling: **D3 REV 3** in `docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md`.

---

## NEW BUG #1 (HIGH) — symlinked subdir silently skipped + marker written

Supervisor-reproduced: `conversations/archive -> /store` → `linked.json`
absent, `failed=[]`, MARKER WRITTEN. `os.walk` default `followlinks=False`
lists the link in `dirnames` but never descends it. (Asymmetry: a symlinked
*file* IS followed today.)

### Fix (D3 REV 3): follow + cycle-guard

In `_copy_dir_verified`: walk with `followlinks=True` AND a visited-realpath
set. On revisiting a realpath → raise (the per-dir catch converts it to a
failed entry; the migration never hangs, never silently loops):

```python
    def _copy_dir_verified(src, dst, name, visited: set[str] | None = None):
        ...
        seen = visited if visited is not None else set()
        for dirpath, dirnames, filenames in os.walk(src, followlinks=True, onerror=...):
            real = os.path.realpath(dirpath)
            if real in seen:
                raise RuntimeError(f"symlink cycle detected at {dirpath}")
            seen.add(real)
            ...
```

(Shape only — thread `seen` through recursion as fits your structure. A
cycle = failed report, no marker — pathological trees fail loud.)

### NEW BUG #2 (MED) — empty dir at a FILE name = permanent failure loop

`new/config.json/` (empty dir) passes the carve-out → every file copy raises
`IsADirectoryError` → failed report, no marker, EVERY retry identical.

### Fix (D3 REV 3)

Carve-out applies to `_MIGRATION_DIRS` names ONLY. For
`_MIGRATION_FILES`/`_MIGRATION_DB_FILES`: any lexists entry (file, dir,
link, fifo) ⇒ content ⇒ no-op (return None). Also: prefix the
negative-status fallback error with the entry name
(`failed.append(f"{name}: {exc}")` — the auditor noted nameless failures).

### NEW BUG #3 (MED) — verify has zero coverage; docstring claims otherwise

The 21-test file passes with `if False:` replacing the verify. The
M5-catchability claim in `test_live_source_append_copies_cleanly`'s
docstring is false.

### Fix

The "no mocks" rule applied to `migrate_v1_config`'s *filesystem logic*; a
UNIT test of `_copy_file_verified` with a monkeypatched seam is legitimate
(auditor's ruling). Add:

```python
def test_copy_file_verified_detects_short_write(monkeypatch, tmp_path):
    """BUG#3 (fix round 2): the byte-count verify must actually fire.
    Monkeypatch os.path.getsize to lie about dst (simulating a short
    write's observable) — with the verify present this RAISES; with it
    disabled (if False:) this test FAILS. Direct unit pin, no filesystem
    contortions."""
```

`monkeypatch.setattr(os.path, "getsize", lambda p: 0 if p == dst else real(p))`
— assert the mismatch raises through the migration path (failed report
names the entry). RED proof: `if False:` the verify → this test FAILS.
Also fix the live-append test's docstring (drop the false claim; point at
the new unit pin).

## Tests (RED-first)

1. `test_symlinked_subdir_followed` (BUG#1) — `archive -> store` →
   `linked.json` copied; marker written; real.json also present.
2. `test_symlink_cycle_fails_closed` (BUG#1b) — `a/link -> a` → failed
   report naming the cycle; NO marker; does not hang (wrap with a timeout
   in the test).
3. `test_empty_dir_at_file_name_noop` (BUG#2) — `new/config.json/` empty
   dir → returns None; nothing copied; no failure loop.
4. `test_copy_file_verified_detects_short_write` (BUG#3) — the unit pin
   above; RED with verify disabled.

## Do NOT change

- Everything from fix round 1 (guards 1-3, per-walk makedirs, created-
  before-copy, len(data) verify line, islink layers, rollback discipline).
- The F6 lexists survivor (defense-in-depth works; informational).

## Battery (paste all)

- `pytest tests/test_config_migration.py -q` (21 + 4)
- `-k config` + ruff/pyright baselines
- RED proofs (4) — including the `if False:` verify-disabled run of the
  whole file (must now FAIL at exactly the new test)

## COMPLETENESS (mandatory)

- [ ] BUG#1 follow+cycle — hunks + 2 RED proofs
- [ ] BUG#2 file-name carve-out + name-prefix — hunks + RED
- [ ] BUG#3 verify unit pin + docstring fix — hunks + `if False:` proof
- [ ] Battery + baselines
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
