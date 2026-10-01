# SPEC-08 SP2 FIX ROUND 2 — re-audit BUG #1/#2 (Debugger, 2026-09-27) + trim ruling

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Supervisor rulings in force:** watermark = appended-through (round 1); NEW — store
is the append-only ledger, JSON is the working file; post-trim divergence is
legitimate and NEVER rebuilt; trimmed-sessions go diverged-flagged + JSON-only.

## BUG #1 — append_delta None-return + pyright regression (gate)

1. `append_delta`: `return last_seq if last_seq is not None else -1`.
2. `append_turn`: narrow `seq` to int before return (`return int(seq)` or assert).
3. NEW test `test_append_delta_full_skip_returns_neg_one`: append rows, then
   append_delta the SAME range again → returns -1 (not None), rows unchanged.
4. Run `pyright utils/transcript_store.py agent/persistence.py` → must be **0
   errors** (the global floor). Paste output. (Supervisor process note: round-1's
   battery omitted pyright — my gap; pyright is now MANDATORY in every battery.)

## BUG #2 — front-trim breaks index identity (compaction is real)

### Store side: `diverged` column + `mark_diverged` + `is_diverged`

- Schema: `sessions` gains `diverged INTEGER NOT NULL DEFAULT 0`.
  **TEST IMPACT:** SP1's `test_schema_has_no_secret_columns` pins the exact 7-column
  set — update the expected set to 8 (this is the sanctioned exception; document in
  the test why the pin changed: new non-secret metadata column).
- `mark_diverged(session_key)`: sets diverged=1 under the tx pattern (creates the
  row if absent). Idempotent.
- `is_diverged(session_key) -> bool`.
- NEW store tests: mark → is_diverged True; mark twice idempotent; delete_session
  clears the flag (fresh session).

### Wrapper side: the append-only guard

In `_append_conversation_delta`, BEFORE computing the delta:

```python
store = _get_store()
wm = store.session_watermark(sk)
if wm >= 0:
    anchor = store.row_at(sk, seq=0)   # NEW tiny store read: (role, content) or None
    first = conv.messages[0]
    append_only_ok = (
        len(conv.messages) >= wm + 1
        and anchor is not None
        and anchor["role"] == (first.role.value if hasattr(first.role, "value") else str(first.role))
        and anchor["content"] == first.content
    )
    if not append_only_ok:
        if not store.is_diverged(sk):
            store.mark_diverged(sk)
            logger.warning(
                "[persistence] %s: conversation history changed shape (context "
                "compaction?) — store delta suspended; session stays JSON-backed "
                "(post-MVP stable-id work will re-sync). Existing store rows "
                "retained as audit ledger.", sk)
        return
# ... existing delta path (guard passed or wm == -1 fresh session)
```

Add `row_at(session_key, seq) -> dict | None` to the store (read-only, current
epoch, the _row_to_dict shape). Rate-limit note: the warning fires only on the
flag TRANSITION because `is_diverged` short-circuits after.

### Wrapper tests

- `test_front_trim_suspends_delta_and_flags`: save 10 → simulate compaction
  (`conv.messages` front-trimmed by 2) → append 5 new → save → store rows STILL
  10 with the ORIGINAL contents (no mis-attribution, no new rows), diverged flag
  set, JSON has all 13 (authoritative). Save AGAIN (+1 msg) → still no store
  append, no second warning escalation (flag already set — assert via caplog count).
- `test_append_only_history_still_dual_writes`: the guard-passes path — fresh
  session, 3 saves with growing lists → all rows present (guard didn't false-fire).
- `test_diverged_session_survives_store_restart`: flag set → new store instance on
  same DB → is_diverged still True (persistence, not in-memory).

## Suggestion folds

- Union-test teeth: extend `test_concurrent_saves_union_preserved` with a
  divergent-content variant — A's tail rows carry distinctive content ("A-tail-i"),
  B's likewise; assert the loser's divergent rows are SKIPPED (not merged): final
  rows 5..7 all carry ONE writer's content, matching first-committer-wins.
- `base_idx` clamp comment: adjust the comment to say the clamp prevents negative
  seq on a corrupt wm but does NOT heal it (IntegrityError → fallback; the
  invariant pin is the real defense). One comment line.

## Verification (paste full output — pyright MANDATORY now)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_transcript_store.py tests/test_agent_persistence.py -v
.venv/bin/python -m pyright utils/transcript_store.py agent/persistence.py
python -m ruff check agent/persistence.py utils/transcript_store.py tests/test_agent_persistence.py tests/test_transcript_store.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: -1 sentinel on full-skip + int-narrow append_turn + pyright 0 errors pasted
- [ ] BUG#2 store: diverged column (schema pin updated to 8) + mark/is + row_at + tests
- [ ] BUG#2 wrapper: append-only guard (anchor + length) + flag transition warning + tests (suspend/flag, no-false-fire, restart persistence)
- [ ] Union divergent-content teeth
- [ ] base_idx comment correction
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
