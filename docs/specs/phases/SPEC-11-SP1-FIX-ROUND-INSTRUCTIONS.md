# SPEC-11 SP1 Fix Round — flat-dir copy, rollback stragglers, unreadable source, verify reference, symlink guard, empty-dir guard

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `utils/config.py` (`migrate_v1_config` + `_copy_dir_verified` +
`_copy_file_verified` + guard-3) + `tests/test_config_migration.py` (seed real
shapes + 6 new tests) + 4 additional drift test files (BUG#7).
Ruling updates: D3 REV 2 (below).

---

## Confirmed by Supervisor reproduction (all three probes)

BUG#1 flat dir → `FileNotFoundError .../conversations/./s1.json`, files not
copied. BUG#4 empty dir → None (suppressed forever). BUG#3 chmod-000 v1 →
`skipped=11` + marker written. Auditor's BUG#2/5/6/7/8 evidenced by probes.

## Fixes

### BUG #1 (CRITICAL) — `_copy_dir_verified` never creates a flat destination

`makedirs` runs only inside `for d in dirnames:`. Fix: create the destination
for EVERY walked level — at the top of each walk iteration:

```python
        rel_base = os.path.relpath(dirpath, src)
        dst_base = os.path.join(dst, "" if rel_base == "." else rel_base)
        os.makedirs(dst_base, exist_ok=True)
```

(the `"" if rel_base == "."` shape handles the root — joining `"."` produces
the `/./` tell-tale in the failure path today).

### BUG #2 (HIGH) — partial dir survives rollback, poisons guard-3

Append the entry name to `created` BEFORE calling `_copy_dir_verified`
(rollback's rmtree handles partial dirs; a pre-failure append is safe because
rollback only removes what exists). Assert in test: after a mid-recursion
failure, the destination `conversations/` is GONE and a retry (post-repair)
migrates.

### BUG #3 (HIGH) — unreadable v1 = silent success + poison marker

Two-part fix (both):
- `os.walk(..., onerror=lambda e: (_ for _ in ()).throw(e))` — a walk error
  becomes the copy failure it is.
- Guard stage: `if os.path.isdir(v1) and not os.access(v1, os.R_OK | os.X_OK):`
  → return `{"copied": [], "skipped": [], "failed": [f"v1 config dir unreadable: {v1}"]}`
  — NEVER the marker.
Unreadable SUBDIR inside a readable v1 → the walk onerror path must catch it
(the per-dir recursion inherits the fix).

### BUG #4 (MED) — empty pre-existing dir permanently suppresses (D3 REV 2)

**Ruling D3 REV 2:** guard-3 fires on a copy-list entry that is a FILE
(any size) or a NON-EMPTY directory. An EMPTY directory is not "content" —
migration proceeds; the copy's `makedirs(exist_ok=True)` absorbs it. The
both-dirs-divergent case (spec §7 row 3) is about CONTENT divergence; an
empty placeholder carries nothing to diverge. SP3's wiring still orders
migration before any dir creation (belt and suspenders).

```python
        for name in _COPY_LIST:
            p = os.path.join(new_dir, name)
            if os.path.isfile(p) or (os.path.isdir(p) and os.listdir(p)):
                return None
```

### BUG #5 (MED) — verify compares against the WRONG reference

`os.path.getsize(src)` re-reads a possibly-mutated live source. Fix:

```python
        if os.path.getsize(dst) != len(data):
            raise ...byte-count mismatch...
```

`len(data)` IS the integrity invariant (what we read is what we wrote). A
live-append after our read now copies cleanly. (The M5 mutant — verify
disabled — becomes catchable: append-to-src-after-read is pure filesystem.)

### BUG #6 (MED) — dangling symlink bypasses guard-3, copy writes outside

- Guard-3 uses `os.path.lexists` (dangling links count as present).
- Before each copy: `if os.path.islink(dst_path): raise` (fail-closed; a
  symlinked destination is never followed). Add to the failure contract.

### BUG #7 (issue) — 4+ unswept drift test files

Sweep (same disclosure treatment as the 5 authorized): 
`tests/test_get_api_key_no_side_effect.py:16`,
`tests/test_bug_fixes.py:177,225`, `tests/test_agent_defs.py:283,344`,
`tests/test_agent_builder_handler.py:170` — fixtures → `develcakes` (or
patch `get_config_dir`, whichever the file's pattern supports). Verify each
still pins real behavior (the auditor's probes E1/E3 showed which asserts
were vacuous — they must become live again).

### BUG #8 (issue) — seeder masks the real v1 shape

`_seed_v1` always fabricates `nested/`. Fix: default seeder = FLAT dirs
(files only, the real shape); a separate `nested=True` variant seeds the
nested shape; BOTH shapes are asserted for the functional dirs. The new
tests below cover the flat path.

## New tests (RED-first; seed the REAL flat shape)

1. `test_flat_conversations_dir_copies` (BUG#1) — flat v1 → both files
   present in new dir, marker written.
2. `test_partial_dir_failure_rolls_back_and_retries` (BUG#2) — FIFO +
   subdir in conversations → failed report, `new/conversations` GONE,
   marker absent; repair (rm FIFO) → retry migrates fully.
3. `test_unreadable_v1_fails_loud_no_marker` (BUG#3) — chmod-000 v1 →
   failed report names it; NO marker; retry after chmod+755 migrates.
4. `test_empty_placeholder_dir_does_not_block` (BUG#4) — empty
   `new/conversations/` + real v1 → migration proceeds.
5. `test_live_source_append_copies_cleanly` (BUG#5) — append to src after
   read (shim or timing hook) → copy succeeds, dst == the read snapshot.
   Also: disable the verify → this test FAILS (M5 now catchable).
6. `test_dangling_symlink_destination_rejected` (BUG#6) — dangling link at
   `new/config.json` → failed report (or None via lexists guard) and NO
   write outside the config dir (assert the outside target absent).

## Do NOT change

- Guard ORDER (v1-absent → unreadable (new) → marker → content); the copy
  list; transcript.db copy-only; never-raises; rollback's created-only
  discipline (extended per BUG#2, not replaced).
- The 5 already-swept drift files.

## Battery (paste all)

- `pytest tests/test_config_migration.py -q` (13 + 6 new)
- `pytest tests/ -k config -q` + the 4 swept files individually
- ruff/pyright on touched files vs baselines
- RED proofs for the 6 tests
- Re-run the auditor's probe shapes (or your equivalents): flat, straggler,
  chmod-000, empty-dir, live-append, dangling-symlink — all fixed shapes
  asserted

## COMPLETENESS (mandatory)

- [ ] BUG#1 flat-dir makedirs — hunk + RED
- [ ] BUG#2 created-before-copy + retry test — hunk + RED
- [ ] BUG#3 onerror + unreadable guard — hunks + RED
- [ ] BUG#4 lexists/content guard (D3 REV 2) — hunk + RED
- [ ] BUG#5 len(data) verify — hunk + RED + M5-catchable proof
- [ ] BUG#6 symlink rejection — hunk + RED
- [ ] BUG#7 4 files swept — hunks + liveness confirmation
- [ ] BUG#8 seeder real-shape — hunk
- [ ] Battery + baselines
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
