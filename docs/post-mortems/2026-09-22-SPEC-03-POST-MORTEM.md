# SPEC-03 Post-Mortem — P11 Memory-Ratchet: Configurable Live Window

**Date:** 2026-09-22 · **Commits:** SP1 `34d6260a` · SP2 `713e57a2` (docs) `077bdd64`
(code) · SP3 `5ac13754` (docs) `0b777ecc` · SP4 riders `80ecb97a` · this close-out
**Spec:** `docs/specs/SPEC-03-MEMORY-RATCHET.md` · **Status:** COMPLETE — all 4 sub-phases

## What shipped (4 sub-phases, per PM's smaller-chunks direction)

- **SP1 — config layer** (`utils/feed_store.py` +237, 27 tests): `get_live_window`/
  `set_live_window` on project-scoped feed-prefs.json (clamp 50–5000, default 300);
  clobber-proof raw-RMW writers under a new prefs flock — the handler's auto-accept
  save can no longer drop `live_window` (round-1 probe: race killed the value 3/5,
  corrupted the file 1/5). No-raise contracts at every stage; seeds born v2-shaped;
  single-suffix lock file; tolerant readers (ValueError-widened for the int-digit limit).
- **SP2 — wiring** (`ui/handlers/feed_handler.py` +42/−3, 8 tests): `_effective_live_window()`
  = min(120, config) resolved once per eviction pass outside `self._lock`; all four
  eviction entry points funnel through it; any failure → warn + fall back 120.
  **Ruling R1 recorded in code:** config can only LOWER the widget cap — default 300
  is a card-retention figure, never a widget raise (post-mortem slope 0.82 vs 0.5
  budget).
- **SP3 — measurement** (`tests/test_feed_retention.py` +398, 8 tests): the 2,000-card
  harness. Widget bound ≤301, disk complete 2,000, accounting identity
  widgets+backlog==2,000, gc census ≤310, speed guard, R1-true edges (120/121, 80/81),
  reload, compaction interplay. 4/4 audit mutations caught by named invariants.
- **SP4 — riders + close-out**: attribution correction (probe loss = flock-timeout
  skip, not the gitignore RMW), compaction-test dependency guard.

## Spec-vs-reality adjudications

1. The spec's §2 view-side pseudocode (`feed_tab.py` window enforcement) was
   **superseded** — v1 MEMRATCHET had already landed the handler-side live window
   (evict/backlog/Load-More). SPEC-03's real work was config + measurement + hardening.
2. Spec's "2,000 cards through real add_card" was amended to `add_cards_batch` —
   proven equivalent for everything measured (audit probe P2a), and the per-card
   path is a test-only pathology at that scale (flock stampede, 1,538/2,000 skips).
3. Spec's "disk keeps everything" qualified: feed_store's own compaction
   (§2.3 window, trigger >2,500 default) is a separate mechanism — documented and
   pinned, not changed.

## Audit trail (13 findings across 4 rounds)

Round 1: 5 ACCEPTED (HIGH clobber/lost-update + corruption; MED big-int escape; vacuous
XDG test inverted; subprocess timeout; mkdtemp leaks). Round 2: 5 ACCEPTED (version-less
seeds; setup-stage OSError escapes; `.lock.lock` name; docstring; `/tmp/proj` leak).
Micro-delta: ACCEPT (root-runner skipif banked → landed as SP2 rider). SP2: ACCEPT
(2 suggestions → SP3 riders). SP3: ACCEPT (2 banked pre-existing funnel divergences;
2 folded riders). Zero findings were misreads this spec — the probe-before-adjudicate
discipline held throughout; Coder's falsifier-by-mutation evidence was genuine every round.

## Banked register (pre-existing, measured this spec — NOT SPEC-03 regressions)

- `append_feed_card` O(n²) snapshot rewrite: ~40 s for 2,000 appends (each re-reads +
  rewrites the whole file). Candidate: journal-only append + periodic fold.
- Per-card persist thread flock stampede (`add_card`): silent skip path loses cards
  at volume (1,538/2,000). No production caller at that scale today; batch path is
  the bulk shape.
- `_ensure_gitignore_entry` shared-`.gitignore.tmp` RMW race: real, non-lossy for
  cards (outcome set entry-added/no-op), worst case drops a concurrent .gitignore line.
- `add_cards_batch` skips the per-card auto-accept check (`add_card`'s `_append`
  has it; `_append_all` doesn't) — LOW funnel divergence, probe P2b.
- `_append_all` has no per-card try/except — a mid-batch raise skips the eviction
  pass for the batch — LOW, probe P2a.
- `B023` late-binding `_cid` at feed_handler.py:958 (latent, dict-lookup-immune today).
- test_feed_handler.py / test_feed_card.py gi/cairo segfaults at clean HEAD (testing
  phase: display server or Xvfb needed).
- load_feed_prefs int-digit-limit ValueError (sibling of the SP1 reader fix).

## Post-mortem data this spec contributed

- The 2,000-card run gives the first **counted** widget-retention baseline: 121 live
  stubs (120 cap + 1 load-more) at 2,000 cards — the widget ratchet is bounded and
  the bound is enforced end-to-end (mutations prove the tests watch it).
- Funnel timing: the add path is O(n) sub-second; all persistent cost is the disk
  snapshot rewrite (banked O(n²)).

## Process notes

- Sub-phase chunking (PM direction) worked: 4 rounds, each committable, each audit
  scoped to a delta — no turn-limit interruptions this spec.
- Env discipline slip: Coder built a /tmp venv this round (vanished-env class);
  re-verified in `.venv`; rule reaffirmed — `.venv` is THE env.
- The /ask 4,096-char cap shaped briefs all spec long: every brief went via file.

## Acceptance criteria (spec §6) — final state

- [x] Live widget count stays ≤ window+1 under unbounded appends (harness, mutations)
- [x] Disk store retains every card; reload hydrates tail + backlog (2,000-run)
- [x] live_window configurable, clamped 50–5000, default 300 (R1: view cap stays ≤120)
- [x] Teardown chunked (≤64) — inherited MEMRATCHET mechanism, untouched
- [x] pytest green (43+61+35 targeted), ruff clean (baselines exact), pyright clean

## Next

**SPEC-04 (R5 Auxilium removal)** — next in the binding chain. Fold-in candidates
from the register: none blocking; the O(n²) persist and batch auto-accept divergence
are natural SPEC-08 (transcript store) / review-layer work respectively.
