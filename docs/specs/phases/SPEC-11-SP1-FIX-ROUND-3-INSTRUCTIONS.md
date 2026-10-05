# SPEC-11 SP1 Fix Round 3 — chain-scoped cycle guard + honest error labeling

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `utils/config.py` (cycle guard rewrite + error labeling) +
`tests/test_config_migration.py` (4 tests). Nothing else.

**Context:** two defects introduced by round 2's fixes. Both Supervisor-
reproduced. This round is surgical — if it introduces new defects, the
Supervisor takes the file over.

---

## BUG #1 (HIGH) — global `seen` set false-positives legitimate DAGs

Diamond (`a → store`, `b → store`) and chains (`a → b → c → store`) are NOT
cycles. Supervisor probe: diamond → `failed=['config.json: symlink cycle…',
'conversations: symlink cycle…']`, nothing migrated, no marker.

### Fix: chain-scoped detection (no global state)

For each walked `dirpath`, resolve the ancestor chain FROM THE COPY-LIST
SOURCE down to it, and check for a repeat WITHIN that one chain:

```python
def _resolved_chain_has_cycle(src: str, dirpath: str) -> str | None:
    """Realpath of each component from src to dirpath; return the first
    repeated real path (a true cycle) or None. A shared target reached via
    two DIFFERENT links (diamond) produces two distinct chains — no repeat
    within either — and is NOT a cycle."""
    chain = []
    cur = os.path.realpath(src)
    chain.append(cur)
    rel = os.path.relpath(dirpath, src)
    if rel == ".":
        return None
    for part in rel.split(os.sep):
        cur = os.path.realpath(os.path.join(cur, part))
        if cur in chain:
            return cur
        chain.append(cur)
    return None
```

In the walk: `hit = _resolved_chain_has_cycle(src, dirpath)` → raise the
named cycle error only when `hit is not None`. DELETE the global `seen`
set. Self-cycles, mutual cycles (a→b→a) still fail (the chain repeats);
diamonds/chains pass (each chain is linear).

(O(depth) realpaths per node — config-dir scale, fine. Disclose if you
find a cheaper correct shape, but correctness first.)

## BUG #2 (MED) — failed report labels PRIOR SUCCESSES

`report["failed"] = [f"{name}: {exc}" for name in created]` iterates
successes; the fallback invents a `migrate_v1_config:` pseudo-entry.

### Fix: label at the catch site with the IN-FLIGHT entry

Per-entry `try/except` around the copy dispatch:

```python
        try:
            ...copy entry `name`...
        except Exception as exc:
            report_failed_name = name
            ...break to rollback...
```

- `report["failed"] = [f"{report_failed_name}: {exc}"]` — exactly ONE
  entry, the one that failed, with ITS exception.
- DELETE the `created`-relabeling comprehension and the
  `"migrate_v1_config: "` fallback entirely.
- The unreadable-v1 guard's existing message already names the dir —
  unchanged.

## BUG #3 (MED) — untested claims; 3 prefix mutants survive

### Tests (RED-first)

1. `test_diamond_links_migrate` — a→store, b→store → migration SUCCEEDS,
   `shared.json` present under BOTH link names, marker written. (RED
   today: the false cycle aborts.)
2. `test_symlink_chain_migrates` — a→b→c→store → succeeds, content under
   `a/`. (RED today.)
3. `test_mutual_cycle_fails_closed` — a→b→a (subdir links) → failed names
   the cycle, NO marker, returns (no hang). (Not RED — new coverage; pin
   it and mutation-verify the guard still catches.)
4. `test_failed_report_names_failing_entry` — two shapes: (a) FIFO at
   `agent.json` (first entry) → failed == exactly one element naming
   `agent.json`, NOT `migrate_v1_config`; (b) verify-mismatch on
   `providers.yaml` via the getsize-lie seam (unit pin already exists —
   extend to the migration path) → failed names `providers.yaml`, NOT
   `agent.json`. (RED today: labels are wrong.)

## Battery (paste all)

- `pytest tests/test_config_migration.py -q` (25 + 4)
- Mutation re-run: R2-3/R2-3b/R2-4 must now be CAUGHT; F1–F6b + R2-1/1b/2
  stay caught (the full matrix, green baseline)
- ruff/pyright baselines; `-k config`

## Do NOT change

- Round-1/2 fixes (guards, makedirs, rollback discipline, carve-out,
  len(data) verify, islink layers). The cycle check MOVES from global-set
  to chain-scoped — that is the only guard-logic change.

## COMPLETENESS (mandatory)

- [ ] BUG#1 chain-scoped guard, seen deleted — hunk + 2 RED
- [ ] BUG#2 catch-site labeling, relabeling deleted — hunk + RED (both shapes)
- [ ] BUG#3 4 tests + full mutation matrix green-baseline run — outputs
- [ ] ruff/pyright
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
