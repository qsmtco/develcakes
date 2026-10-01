# SPEC-08 SP2 FIX ROUND 3 — re-audit BUG #1/#2/#4 (Debugger, 2026-09-27)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Rulings unchanged:** store = append-only ledger, JSON = working file; divergence
flags + suspends, never rebuilds. This round hardens the GUARD only.

## BUG #1 (HIGH) — single-point anchor bypassed by middle trims (keep_first=2)

The seq-0 anchor can't catch a trim that preserves index 0. Production-confirmed:
the real `DefaultContextStrategy().compact()` (keep_first=2) bypassed the guard —
`is_diverged: False`, 9 stale rows + 4 real turns absent.

**Fix — anchor on the last committed row (the probe with teeth):**

In `_append_conversation_delta`'s guard:

```python
if wm >= 0 and len(conv.messages) >= wm + 1:
    last_committed = store.row_at(sk, seq=wm)      # the boundary row
    boundary = conv.messages[wm]                   # what JSON claims is at wm
    anchor0 = store.row_at(sk, seq=0)              # front anchor (cheap, keep)
    append_only_ok = (
        last_committed is not None
        and anchor0 is not None
        and _same_turn(last_committed, boundary)
        and _same_turn(anchor0, conv.messages[0])
    )
```

Add a tiny module-level `_same_turn(row: dict, msg) -> bool` comparing role +
content exactly (the same normalization the delta shaper uses). Any trim anywhere
in `[0..wm]` shifts `messages[wm]` → caught. Appends above wm don't move it →
passes. Keep the emptiness/length ordering per BUG #2 below.

## BUG #2 (MEDIUM) — IndexError on empty messages; emptiness check is dead code

`first = conv.messages[0]` runs before the emptiness clause; a full clear crashes
into the generic fallback swallow and never flags.

**Fix — order of operations in the guard:**

```python
if not conv.messages:
    # A fully-cleared conversation no longer reflects the ledger — same
    # divergence handling: flag once, suspend delta, JSON stays authoritative.
    if not store.is_diverged(sk):
        store.mark_diverged(sk)
        logger.warning(...)
    return
```

…then the wm>=0 path, then the anchors. The `len >= wm+1` clause still runs
after emptiness is handled (a non-empty list shorter than wm+1 is also divergence:
front-trim shrink — flag + return, same path).

## BUG #4 (LOW) — test only covered the one trim shape the old anchor caught

**Tests (extend the existing ones, keep names stable):**

1. `test_front_trim_suspends_delta_and_flags` — ADD the middle-trim variant:
   save 10 → `del conv.messages[2:4]` (preserves 0 and 1 — the keep_first=2
   production shape) → append 5 new → save → assert `is_diverged() is True`,
   store still exactly the original 10 rows (contents identical — no
   mis-attribution), JSON has all 13. This is the BUG#1 kill test: it must go
   RED against the current single-anchor code (prove it, then green after).
2. NEW `test_full_clear_flags_diverged` (BUG#2): save 1 msg → `.clear()` →
   save → no exception escapes, `is_diverged() is True`, exactly one warning
   (caplog), store row retained.
3. NEW `test_real_strategy_compaction_flags` — production reachability pin:
   drive the REAL `DefaultContextStrategy().compact()` (token-budget small
   enough to trigger) over a saved conversation with appended post-compact
   turns → save → `is_diverged() is True`, zero mis-attributed rows (store
   contents == pre-compact snapshot).
4. Keep `test_append_only_history_still_dual_writes` green (no false-fire) —
   extend it with a grow-past-wm save (appends above the boundary must not
   trip the new anchor).

## BUG #3 (LOW) — register only, no code

Content-based anchoring can false-positive if index-0 content is stubbed in
place (`prune_tool_outputs`, context_strategy.py:431). Document as a known
limitation in the guard's comment (one line: "content-based anchor;
in-place content stubbing at the anchors is a known false-positive —
stable-id keying is the post-MVP fix"). Do NOT add code for it.

## Verification (paste full output — pyright MANDATORY)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_transcript_store.py tests/test_agent_persistence.py -v
.venv/bin/python -m pyright utils/transcript_store.py agent/persistence.py
python -m ruff check agent/persistence.py utils/transcript_store.py tests/test_agent_persistence.py tests/test_transcript_store.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: seq-wm boundary anchor + seq-0 anchor + _same_turn helper
- [ ] BUG#1 kill test: middle-trim variant RED-then-GREEN proof pasted
- [ ] BUG#2: emptiness-first ordering + full-clear test (one warning, flagged)
- [ ] BUG#4: real-strategy compaction pin + no-false-fire growth extension
- [ ] BUG#3: one-line limitation comment (no code)
- [ ] Full battery green incl. pyright 0 + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
