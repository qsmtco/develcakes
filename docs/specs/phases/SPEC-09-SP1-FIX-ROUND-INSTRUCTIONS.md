# SPEC-09 SP1 FIX ROUND — audit BUG #1–#5 + NaN guard (Debugger, 2026-10-02)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Ruling (BUG#1):** disk owns the lease. The handler's stale-snapshot writes must
PRESERVE on-disk leases; only the lease API may clear one. claim/release VERIFY
their own persistence (no phantom success).

## BUG #1 (CRITICAL) — split-brain writers clobber live leases

The handler's `_persist()` rewrites work.json from long-lived in-memory objects
whose `lease` is stale-None → any `/work priority|assign|status|done` erases a
live claim (auditor's probe D: second holder then claims the same unit).

**Fix (three parts):**
1. `save_work_units(units, project_path, *, preserve_leases=True) -> bool`:
   TRUE disk-owns-the-lease merge (Debugger probe G correction — the original
   directional rule left the mirror bug: a stale NON-None in-memory lease could
   resurrect a release or overwrite a new holder): for each unit being written
   that HAS an on-disk counterpart → written lease := the ON-DISK lease when it
   is non-None AND LIVE, else None. The in-memory `.lease` is IGNORED entirely
   for existing units (whatever it says, stale or fresh). New units (no on-disk
   record) keep their in-memory lease (None for hand-built units). Deletions
   unaffected. Debug log on any carry (unit id + holder).
2. `release_work` writes with `preserve_leases=False` (its lease=None IS the
   intent — the merge must not resurrect it). `claim_work` writes the actual
   lease (non-None) — either flag value works; pass False for clarity.
3. **Structural requirements (auditor's re-audit checks — build to these):**
   (a) the preserve_leases disk-load MUST happen inside the SAME `_LEASE_LOCK`
   acquisition as the write (a load outside the lock is a TOCTOU on the merge
   itself); (b) the merge reads via `_load_valid_work_json`, NOT
   `load_work_units` — saves must not re-trip `_work_init_counter` (module
   init/first-run semantics stay save-path-invisible).
4. `save_work_units` returns `bool` (True = persisted; False = the silent
   no-op path — dir failure). Existing callers ignore it (backward compatible).

**Tests (the auditor's demanded integration pin + more):**
- `test_claim_survives_handler_mutation` — the probe-D shape through the REAL
  WorkHandler: claim(coder) → `/work priority <id> high` → fresh-load:
  lease INTACT, assert_lease True, second holder's claim → None.
- `test_release_survives_handler_mutation` — release → handler mutation →
  lease stays cleared (release not resurrected by the merge).
- `test_stale_nonnone_lease_not_written_back` — the probe-G mirror: in-memory
  object holds a stale lease (claimed long ago, released on disk by the API);
  handler persist → on-disk lease stays None (the stale claim does NOT
  resurrect); a new holder's live on-disk lease is NOT overwritten by a stale
  in-memory holder's lease.
- `test_expired_lease_not_preserved` — on-disk lease EXPIRED (frozen clock past
  ttl) → handler mutation → lease dropped (preserve is for LIVE leases only).
- `test_save_units_returns_false_on_dir_failure` — patched _ensure_crabcakes_dir
  raising → False, no raise.

## BUG #2 — phantom claim/release on silent persist failure

**Fix:** `claim_work` checks save's return: False → return None (log warning
"claim not persisted"). `release_work`: False → return False. No verify-read
needed — the bool IS the signal.
**Test:** the auditor's probe E shape — patched dir failure → claim None,
release False, nothing in memory claims success.

## BUG #3 — corrupt lease drops the WHOLE unit

`WorkUnit.from_dict`: wrap ONLY the lease parse in try/except ValueError →
log warning naming the unit + field, set `lease=None`, KEEP the unit. The
load-loop's per-record skip stays for other corruption.
**Test:** probe A shape — unit with `lease.holder: 12345` → unit PRESENT with
lease None; re-save heals the record (no corrupt residue).

## BUG #4 — concurrency pin has no power

Rebuild `test_atomicity_under_concurrent_claims`: **4 threads** (auditor's data:
no-lock 4-thread → 155/200 double-winner + 17/200 torn; with lock 0/0), 50
trials, assert exactly-one-winner every trial + file parses. PLUS a sensitivity
meta-check in the same test: monkeypatch the lock to a no-op context manager and
assert the RACE IS DETECTED in that configuration (run a few trials, expect ≥1
violation — if the no-lock run ever passes 10/10 trials cleanly, SKIP with a
message, don't fail-flake; the pin's power is demonstrated, not guaranteed per-run).

## BUG #5 — unit-id form mismatch

`_validate_unit_id` → `_normalize_unit_id`: accept `"3"`, `"#3"`, `"00000003"` →
canonical zero-padded 8 (`str(int(id.lstrip('#'))).zfill(8)`); non-numeric →
ValueError. Docstring states the canonical form. The lease API never silently
misses on form.
**Test:** `claim_work(proj, "#3", ...)` and `claim_work(proj, "3", ...)` both hit
unit 00000003 (probe F shape).

## Minor — NaN/inf ttl

`_validate_ttl`: add `math.isfinite(ttl)` → else ValueError.
**Test:** float('nan') and float('inf') both raise.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_work_persistence.py tests/test_work_unit.py -v
.venv/bin/python -m pyright utils/work_persistence.py models/work_unit.py ui/handlers/work_handler.py
python -m ruff check utils/work_persistence.py models/work_unit.py ui/handlers/work_handler.py tests/test_work_persistence.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: preserve_leases merge + release exemption + bool return + the handler-integration pin + expired-not-preserved + dir-failure test
- [ ] BUG#1 structural: disk-load inside the SAME _LEASE_LOCK as the write; merge reads via _load_valid_work_json (not load_work_units — no _work_init_counter re-trip)
- [ ] BUG#2: claim/release honor the bool (probe-E tests)
- [ ] BUG#3: lease-only corruption keeps the unit (probe-A test + heal pin)
- [ ] BUG#4: 4-thread/50-trial pin + lock-removal sensitivity check
- [ ] BUG#5: id normalization (#3/3/00000003) + non-numeric raises
- [ ] NaN/inf ttl guard
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
