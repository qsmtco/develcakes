# UIRESP2 PHASES 1–3 — POST-DEPLOYMENT LIVE VERIFICATION

**From:** Lt. Qrusher (independent verification)
**Date:** 2026-09-11 ~21:07–21:13 PDT
**Deployed build:** `6b616d1` (includes the F1 amendment) — live process PID 1233555, started 20:50:12
**Load condition:** REAL agent run — team working on Phase 5
**Method:** read-only `/proc` sampling + file-level instrumentation. No repository files modified.

---

## 1. Headline — the freeze is gone under real load

| Metric | Before (pre-fix, agent run) | **After (agent run, Phase 5)** |
|---|---|---|
| GTK main-thread CPU | ~71% of a core, pinned | **12.1%** (busiest thread in the process) |
| Main-thread samples in feed code | **100%** (json.load ×10, json.dump ×8, `_acquire_lock` ×9) | **0%** — idle in `poll_schedule_timeout` |
| Process CPU (all 17 threads) | ~83% | **20.8%** |
| feed.json | 15.5 → 17.0 MB, growing | **4.1 MB**, stable |
| Feed cards | 10,924 | **3,135** |

The main thread is the busiest thread during the run (12.1%) but that is normal GTK render load — it is not pinned, and it is not in the feed path.

## 2. Hot-path cost, verified on the live feed

| | |
|---|---|
| `append_card_update` (journal path) | **0.096 ms/update** — old path: 620 ms → **~6,500× faster** |
| feed.json rewritten by a card *update* | **NO** — 50 updates, mtime and byte size both unchanged |
| `load_feed` merging journal | 48 ms for 3,168 cards |
| Post-fix compaction | 0.16 s |

## 3. F1 amendment verified empirically

The earlier report's **F1** (stale `needs_approval` pins defeating the sliding window) is fixed in `6b616d1`:

- Pin reasons on the live feed: **`accepted: 1514` only**
- The **2,335 stale `needs_approval` pins are gone** — that was the set holding the window open
- Retention is now driven by real recorded decisions, as intended

## 4. Compaction runs off the main thread

`_drain_persist_queue` (called by `_persist_loop`, the writer thread) is the only path to `compact_feed`. Confirmed by observation: main thread stays at 12.1% while a compaction runs.

## 5. Two remaining findings (minor — not regressions, not UI-blocking)

### F-A — the compaction trigger is permanently true

`_maybe_compact` fires when `n > FEED_WINDOW_DEFAULT * 1.25` = **2,500 cards**. But the achievable retention floor is **3,135 cards** (2,000 window + 1,514 pinned, minus overlap) — so the condition is true forever.

Measured: **one compaction per 60 s** (the rate limit), each performing a full 4 MB load + atomic rewrite + journal truncate, and pruning **0–17 cards**.

Effect: permanent low-grade churn that never achieves anything, because the feed can never fall below the threshold.

**Suggested fix:** trigger on `n > window + pinned_count + margin`, or key the trigger off journal-line count rather than the retained card count.

### F-B — new-card creation is still O(n)

`append_feed_card` (full read → parse → append → atomic rewrite) is still used for **new cards** at `feed_handler.py:828/831/958/961/1360`. The Phase 2 journal covers *updates* only.

Measured during the run: **26 feed.json rewrites in 60 s** (25 new-card appends + 1 compaction), file growing 4.137 → 4.154 MB.

**Mitigating:** it runs on background daemon threads (`_persist`, `_persist_all`), so the UI is unaffected. **Not mitigated:** it is O(n) per card on a 4 MB file, and it spawns a thread per call.

**Suggested follow-up (future phase):** extend the journal to new-card inserts, or batch more aggressively.

---

## 6. Instrument error, owned

My first live watcher read `/proc/PID/stat` and labelled it "main thread". That file reports **process-wide** CPU (verified: 14,903 ticks process-wide vs 10,372 summed across live threads — the gap is exited threads folded into the group leader). Its 100–124% readings were the whole process, not the main thread, and are not evidence of a stall.

The trustworthy figures above come from per-thread `/proc/PID/task/<tid>/stat` sampling. Watcher script was stopped; it should not be relied on as written.

---

## 7. Verdict

**Phases 1–3 do what they claimed, under real load.** The main thread no longer performs feed I/O, the feed is bounded, and the F1 amendment works. F-A and F-B are follow-up work, not defects in this delivery.
