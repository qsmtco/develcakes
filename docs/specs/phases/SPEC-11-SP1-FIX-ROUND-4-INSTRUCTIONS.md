# SPEC-11 SP1 Fix Round 4 — destination-reentry guard + atomic marker

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `utils/config.py` + `tests/test_config_migration.py`. Surgical.

**Context:** closure audit closed all three round-3 targets; two PRE-EXISTING
failure-path gaps surfaced (not round-3 regressions — the takeover clause does
not apply). Both strand user data — the one outcome this migration exists to
prevent. Supervisor ruling: fix now, not SP4.

---

## BUG #4 (MED, pre-existing) — symlink into the destination = runaway recursion

`v1/conversations/loop -> <new config dir>` (dangling at plant time, valid
once migration creates the dir): `followlinks=True` descends into the
destination being written; each level makedirs a fresh dir; every dirpath's
chain is genuinely new → the cycle guard never fires. Auditor probes:
12s-timeout alive with 357 entries; long run terminates only at
ENAMETOOLONG after **94.8s** with ~600 nested dirs written into the user's
config dir (round-2 guard: 1.1s on the same input).

### Fix: forbidden-root rejection (exact, cheap)

Thread the migration's new config dir into `_copy_dir_verified` as
`forbidden_root` (computed once as `os.path.realpath(new_dir)` in
`migrate_v1_config`). Per walked dirpath, AFTER the chain check:

```python
        real_dp = os.path.realpath(dirpath)
        if real_dp == forbidden_root or real_dp.startswith(
                forbidden_root + os.sep):
            raise RuntimeError(
                f"copy destination re-entered at {dirpath} "
                f"(under {forbidden_root})"
            )
```

Covers the whole class (link into own dst, into the config root, into
ANOTHER entry's dst — cross-entry self-reference). No false positive:
legit walked paths are under `src`, never under the new config dir (only a
bridging symlink gets you there — which is the bug). Bounded: fires on
first reentry.

### Test (RED-first)

`test_destination_reentry_fails_fast` — plant
`v1/conversations/loop -> <new_dir>` (dangling) + real v1 data → migrate →
assert: failed report (names the reentry), NO marker, returns in < 5s
(wrap with a deadline), and post-rollback the new dir holds none of the
nested loop dirs (rollback discipline held).

## BUG #5 (LOW-MED, pre-existing round-1 logic) — marker write-after-create poisons the one-shot

`open(marker, "w")` creates the file; a subsequent `write()` failure
(ENOSPC) leaves a 0-byte marker that survives rollback (never in
`created`) → every future run is a silent no-op; data stranded forever.
Auditor probe reproduces the strand.

### Fix: temp + atomic replace + except-cleanup

```python
        marker_tmp = marker_path + ".tmp"
        with open(marker_tmp, "w") as fh:
            fh.write("migrated from v1 (crabcakes)\n")
        os.replace(marker_tmp, marker_path)
```

In the except/rollback block: `if os.path.exists(marker_tmp):
os.remove(marker_tmp)` (and best-effort remove of a partial marker_path if
it exists — belt and suspenders for any open("w")-then-fail shape).

### Test (RED-first)

`test_marker_write_failure_retries_clean` — monkeypatch the marker write
to raise after open (the unit-pin seam: patch the file object's write, or
open at the marker path) → assert: failed report, NO marker AND no `.tmp`
survive, repaired retry migrates fully (marker present, data copied).

## Withdrawn registration (record in context.md)

Coder's "non-copy-list FIFO/socket in a v1 subdir blocks on open" —
FALSIFIED by the auditor: `os.path.isfile` is False for FIFOs; the
existing gate at the walk raises; probes fail closed in <1s. Withdrawn.

## Do NOT change

- The chain guard, labeling, guards 1–4, rollback discipline, islink
  layers, verify. The doubled-prefix shape stays (auditor: acceptable,
  keeps the exact-equality pins meaningful).

## Battery (paste all)

- `pytest tests/test_config_migration.py -q` (29 + 2)
- Mutation matrix stays green (no new survivors; R-series intact)
- ruff/pyright baselines; `-k config`
- Auditor's two probes as RED→GREEN evidence (`runaway_into_newdir` <5s;
  marker-poison retry clean)

## COMPLETENESS (mandatory)

- [ ] BUG#4 forbidden-root guard — hunk + RED (reentry fails <5s, rollback clean)
- [ ] BUG#5 atomic marker + except-cleanup — hunk + RED (no marker/tmp; retry migrates)
- [ ] Withdrawn-registration note
- [ ] Battery + matrix + baselines
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
