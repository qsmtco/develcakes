# SPEC-09 SP1 — Work Leases (claim / release / assert, TTL)

**Spec:** SPEC-09 §2 work_persistence extension + pre-flight D3 (lease lives IN the
unit record; single atomic write; no sidecar file).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP0 (6b3beb72).
**Touches:** `utils/work_persistence.py`, `tests/test_work_persistence.py` (extend).
**Does NOT touch:** runtime, ARH, worktree_manager (later phases).

---

## Verified facts (HEAD 6b3beb72)

- `WorkUnitStore` persists `.crabcakes/work.json` via atomic tmp+rename under its
  own lock (the pattern SP3's transcript store adopted). Zero claim/lease refs today.
- Work units carry `id`, status fields, etc. — READ the dataclass before extending;
  do not invent field names.
- `/work start #N` exists at the command layer but has no lease concept — SP1 is
  the API ONLY; wiring rides SP3.

## Edit 1 — lease API

```python
@dataclass
class WorkLease:
    unit_id: int
    holder: str            # session_key
    claimed_at: float      # time.time() at claim
    ttl_seconds: float     # default 900.0 (spec)

def claim_work(store, unit_id: int, holder: str, ttl: float = 900.0) -> WorkLease | None:
    """Claim if unclaimed OR the existing lease is expired. None = refused.
    Double-claim by a DIFFERENT holder while live → None.
    Re-claim by the SAME holder while live → refreshes claimed_at (heartbeat),
    returns the new lease (idempotent re-entry, not a refusal)."""

def release_work(store, unit_id: int, holder: str) -> bool:
    """Release iff holder matches (or lease expired). False = not holder/not held.
    Never raises on unknown unit."""

def assert_lease(store, unit_id: int, holder: str) -> bool:
    """True iff holder holds a LIVE lease (not expired). Pure check, no writes."""
```

Rules:
- Lease stored INSIDE the unit record under the store's lock (D3) — one atomic
  write; no sidecar.
- TTL expiry is computed at READ time (claim/assert compare `now - claimed_at <
  ttl`), NOT by a background sweeper — an expired lease is simply re-claimable.
- Unknown unit_id → claim returns None; release False; assert False. Never raises.
- TTL <= 0 → ValueError at claim (programmer error, not runtime).

## Edit 2 — tests (extend tests/test_work_persistence.py)

1. `test_claim_release_roundtrip` — claim → assert True → release → assert False.
2. `test_double_claim_refused` — A claims; B claim → None (spec AC).
3. `test_same_holder_reclaim_refreshes` — A claims; A re-claims → new lease,
   claimed_at advanced (monkeypatch time.time or compute).
4. `test_ttl_expiry_reclaimable` — claim with tiny ttl; advance time (patch);
   B claim → SUCCEEDS (spec AC); assert A now False.
5. `test_release_wrong_holder_refused` — A claims; release(unit, B) → False; A
   still holds.
6. `test_expired_release_by_anyone` — expired lease; B release → True (an expired
   lease is nobody's).
7. `test_unknown_unit_never_raises` — claim/release/assert on id 9999 →
   None/False/False.
8. `test_ttl_zero_raises` — ValueError.
9. `test_lease_persists_across_store_reload` — claim; NEW WorkUnitStore on the
   same file; assert holds (the lease is durable, not in-memory).
10. `test_atomicity_under_concurrent_claims` — two threads, barrier, same unit:
    exactly ONE wins, the other None; store file valid JSON after.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_work_persistence.py -v
.venv/bin/python -m pyright utils/work_persistence.py
python -m ruff check utils/work_persistence.py tests/test_work_persistence.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: WorkLease + claim/release/assert per contract (in-unit, read-time TTL, same-holder refresh)
- [ ] Edit 2: 10 tests incl. concurrency + durability
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
