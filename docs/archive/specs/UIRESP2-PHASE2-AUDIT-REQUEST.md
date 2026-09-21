# PHASE 2 AUDIT REQUEST — UIRESP2

**To:** Debugger
**Mode:** `prompts/adversarialDebugger.md`, 11 sections
**Scope:** `utils/feed_store.py` (full rewrite of the persistence core: :39-58 constants, :151-184 bounded lock, :186-254 shared parse/overlay, :256-352 append_card_update, :352-398 replay, :399-414 line count, :416-482 load_feed, :483-506 save_feed, :507-570 append_feed_card+_maybe_compact, :571-654 compact_feed, :655-676 update_feed_card, :677-730 legacy) + `tests/test_feed_store.py` (37 new + 1 rewrite) + the 1-line `tests/test_low12_13_feed.py` monkeypatch addition.
**Spec contract:** `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` §2.2 + §10.

## Supervisor pre-findings (verify or break — highest priority)

1. **SPLIT LOCK FILES (suspected HIGH/CRITICAL):** `append_card_update` acquires `_acquire_lock(jp)` (:309, jp = journal path → lock file `feed-updates.jsonl.lock`), while `compact_feed` (:592) and `load_feed` (:440) acquire `_acquire_lock(path)` (feed path → `feed.json.lock`). These are DIFFERENT flock files → journal appends and compaction do NOT mutually exclude. Loss window: append flushes line X (returns True) between compact's journal read and its truncate → X absent from the folded snapshot AND truncated from the journal → **update lost despite True**. The spec's uniform rule (§2.2.3 "every feed_store mutation (snapshot or journal) holds the feed flock"; §2.2.4 "a concurrent compaction can never truncate between our append and its fold") — and r2#16's fix — mean ONE lock. Also check: load_feed's journal read (under feed lock) can see an in-flight torn append (stale-read tolerable?) and the torn-tail repair racing compact's truncate. **Trace production call sites: the writer thread serializes update-path compaction, but `add_card`'s per-card persist threads call `append_feed_card` → `_maybe_compact` → `compact_feed` (feed lock) while the writer appends (journal lock) — concurrent in production.**
2. **Tail-repair deviation (accepted by me — sanity-check it):** spec §2.2.4/§7 said "prepend \n on torn tail"; Coder traced that as LOSSY (the damaged bytes would shadow every subsequent record at replay AND get baked into the snapshot at fold) and implemented repair instead (`_tail_is_complete_record`: complete-record-missing-newline → preserve + \n; garbage → truncate). Verify the repair logic itself (partial JSON object? record with no newline and valid JSON? binary garbage? empty journal?) and that replay/compact still honor stop-at-first-bad-line for MID-journal damage.
3. **test_low13_update_feed_card_atomic patch (accepted by me):** one added `json.dumps` monkeypatch so the injected crash stops both the journal hot path and the legacy RMW, preserving the test's "durable state survived an interrupted write" intent. Confirm no assertion changed and no other test was touched.

## Also probe

- The tri-state flow end-to-end against the Phase-1 writer (`ok is False` → defer; `ok is None` → INFO drop; True → proceed) — any misclassification left?
- The bounded lock: `_acquire_lock` retries `time.sleep(_LOCK_RETRY_DELAY)` = 0.05 s × up to 40 iterations within 2 s — CPU behavior acceptable? `os.close(fd)` on every timeout — fd leak impossible?
- `load_feed` size-scaled timeout: `min(60.0, 10.0 + size/1_000_000)` — 13.9 MB → ~23.9 s. Fine.
- `_maybe_compact` rate limit: timestamp before size check; `compact_feed` records on success — double-record consistent?
- Compaction crash-ordering: snapshot replace then journal truncate — verify code order; replay idempotence.
- The 40-failure baseline claim: Coder ran with `--ignore=tests/test_agent_runtime.py` (documented pre-existing OOM) and got identical sets +37 passed. Acceptable evidence pattern (AC3 discipline allows documented exclusions).
- Tests: red-first evidence for the 33; `make_feed(n)` builder; the two Phase-1-race-closure tests.

## Deliverable

BUG #[N] format, severity-tagged, citations, traced. Explicit "no bugs found" per area. On pre-finding #1: confirm or refute with a traced interleaving.
