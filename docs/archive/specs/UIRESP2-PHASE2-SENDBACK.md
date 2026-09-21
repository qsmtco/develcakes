# PHASE 2 SEND-BACK — Split-Lock Fix (1 line) + Regression Test

**To:** Coder — fix this, then STOP for re-audit.
**From:** Supervisor. Debugger audit: 1 CRITICAL confirmed (split-lock inodes), 2 MEDIUM (folded below), 17 areas clean.

## The bug (Debugger-confirmed, traced)

`append_card_update` at :309 acquires `_acquire_lock(jp)` — jp = journal path → lock file `.crabcakes/feed-updates.jsonl.lock`. Every other mutation (`load_feed` :440, `save_feed` :483, `append_feed_card` :507, `compact_feed` :592, `_update_feed_card_legacy` :695) acquires `_acquire_lock(path)` on the FEED path → `.crabcakes/feed.json.lock`. **Two different inodes; `fcntl.flock` is per-inode; no mutual exclusion.**

Loss interleaving (traced by Debugger + me): the writer appends journal line X (returns True) *after* `compact_feed` read the journal for its fold but *before* compact's truncate → X is absent from the folded snapshot AND truncated from the journal → **update permanently lost despite True**. Production-reachable: `add_card`'s per-card persist threads (`append_feed_card` → `_maybe_compact` → `compact_feed`, feed lock) race the writer's journal appends (journal lock) — no serialization.

This violates the spec's uniform rule (§2.2.3: "every feed_store mutation (snapshot or journal) holds **the feed flock**"; audit r2 #16's fix) — one lock inode, every mutation.

## The fix

`utils/feed_store.py` :309: `_acquire_lock(jp)` → `_acquire_lock(_feed_path(project_path))`.

Keep the lock-busy WARNING message (update the text to say "feed lock" not "journal lock"). Everything else in the method unchanged (the tail repair stays — Debugger confirmed it correct across 7 edge cases).

## Also fold in (Debugger MEDIUMs, both small)

1. **`load_feed` lock-free fallback reads the journal unprotected (:440-482)** — after the one-line fix, the fallback's journal read is still outside any lock. While the FEED lock is held by others, the fallback can read a journal mid-append. Acceptable residual per spec §2.2.3 ("narrow documented race, next load consistent") — **no code change**; add one clarifying comment at the fallback site citing this acceptance.
2. **Deferred-phase `None` handling has no INFO log** (the queue-phase logs INFO on None; the deferred phase treats None as success-and-remove silently — I flagged this in my Phase-1 verification). Add the same INFO log to the deferred phase's None path for symmetry: `persist: card %s no longer exists in %s; deferred update dropped (pruned?)`.

## Regression test (red-first against the CURRENT tree)

Add to `tests/test_feed_store.py` (new class `TestSingleLockInode`):
- `test_journal_append_and_compaction_share_one_lock_inode`: hold `feed.json.lock` externally (acquire the flock on the FEED lock path the way the existing timeout tests do), then call `append_card_update` → must return False (lock busy, skipped — NOT a successful write under a different lock). On the broken tree it returns True (writes under the journal lock despite the feed lock being held) — that's your red.
- `test_no_update_lost_when_append_interleaves_compaction`: deterministic interleaving via monkeypatched `_replay_journal` in `compact_feed` (patch feed_store._replay_journal with a wrapper that, on first call, appends a new journal line X via the raw file API — simulating the writer landing mid-fold — then returns the original overlay), run `compact_feed`, then assert `load_feed` still sees X's effect after the truncate. On the broken tree X is lost (red); on the fixed tree compact blocks → X survives (green). If the deterministic interleave proves hard to stabilize, an acceptable alternative: instrument `_acquire_lock` (thread-local reentry/lock-path recorder) and assert EVERY mutation path's recorded lock path equals the feed lock path (the existing `test_no_nested_flock_acquire_anywhere` harness pattern).

## Verification (paste full output)

```bash
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_feed_handler.py -q
grep -n "_acquire_lock(jp)" utils/feed_store.py   # → 0 matches
grep -c "_feed_path(project_path))" utils/feed_store.py  # sanity — feed lock sites
```

Report COMPLETENESS for: the 1-line fix, both MEDIUM folds, both tests red→green. Then STOP for Debugger re-audit.
