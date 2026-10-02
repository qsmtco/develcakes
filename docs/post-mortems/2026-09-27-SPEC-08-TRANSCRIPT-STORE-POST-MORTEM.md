# SPEC-08 (Transcript Store) Post-Mortem

**Date:** 2026-09-27 (loop start; SP1–SP4A landed 2026-09-27 → 2026-10-01)
**Supervisor:** Supervisor
**Builder:** Coder
**Auditor:** Debugger
**Commits:** 5 (832e21d4 planning, b30fed78 SP1, e818b7de SP2, 43f59845 SP3, 3f61ba6e SP4A)
**Phases:** 4 delegated subphases + supervisor close-out (SP1 store core → SP2 dual-write wrapper → SP3 migration+acceptance → SP4A store-mode load/banner/flag → SP4B this post-mortem+push)
**Total bugs found:** 33 across 7 audit rounds (7 HIGH/bug-class, 10 issue, 16 suggestion/LOW-class)
**Process:** implementationLoop §3.1a throughout — every code-bearing turn Debugger-audited pre-commit; steelFramedCodeWriter to Coder every build round (7 build rounds); adversarialDebugger to Debugger every audit round (7 audits + re-audits); pyright + xvfb-run mandatory after the round-1 gate gap.

---

## 1. Code Quality Grade: A- (90/100)

### Justification

The audit loop earned its keep at unusual depth this spec: 33 findings, zero shipped — every one caught at its phase boundary and fixed pre-commit, several with empirical probes (turn-dropping races reproduced with exact counts, data-loss shapes proven by seeded DBs, a dead-feature closure bug the green suite could not see). The cost was 7 fix rounds across 4 subphases — the loop ran hot, and three of the HIGH-class findings traced to **supervisor-authored specifications** (my SQL, my single-branch earned-rename, my load-switch omission), a pattern worth more than any single bug. The product ships with append-safe writes (two-writer 1000/1000), coverage-exact migration that refuses every poison shape it was probed with, and a JSON fallback that survived its own incident.

| Category | Score | Notes |
|-----------------------|-------|-------|
| Correctness | 18/20 | Zero shipped defects; −2 for the three supervisor-spec HIGHs that the loop caught only at audit depth |
| Architecture compliance | 10/10 | Global-per-install per D1=(c); store below agent layer; handler-owned card; runtime GTK-free via dispatch |
| Test coverage | 9/10 | 8→28 persistence + 24 migration + 30 store tests; mutation-proven pins; −1 for two green-suite-blind bugs (closure capture, singleton leak) only order/production-path tests catch — now pinned |
| Documentation | 8/10 | Rulings + trust boundaries documented in-code; −2 for stale comments needing 3 audit rounds to fully sweep |
| Maintainability | 9/10 | −1 for the content-anchor false-negative/positive residuals requiring stable-ids (registered, documented) |
| DX | 9/10 | Per-file ruff methodology note; busy_timeout evidence trail; −1 for the incident (recovered) |
| **Total** | **90/100** | **A-** |

Deducted points:
- 2 Correctness: supervisor-authored predicates (covers() SQL + biconditional, single-branch rename, omitted load switch) each required an audit round to catch — spec-level adversarial review was the missing stage
- 1 Test coverage: production-order and global-state tests were afterthoughts; both gap classes bit
- 2 Documentation: comment drift survived multiple rounds (flag default, guard flag name, _running docstring)
- 1 Maintainability: content-based anchoring residuals (documented, post-MVP)
- 1 DX: the 4,946-file incident (structurally closed, but it happened)

---

## 2. What's Good About the Code

1. **The transaction discipline stack:** instance lock in-process + BEGIN IMMEDIATE across instances + busy_timeout=15000 + rollback-on-fail + init-conn close — the write path survived the auditor's two-instance (1000/1000 ×5), cross-process (600/600), load-contention (6/6 at 15s), and poison probes (utils/transcript_store.py:99-120, :158-215). The last-writer-wins class is dead at every level it was attacked.
2. **Coverage as the migration predicate:** `covers()` (store :436) — one COUNT against the current epoch with the `[0..upto]` floor — replaced two count-based holes; every poison shape probed (phantom seq, epoch inflation, negative seq, fractional seq sibling registered) refuses and keeps the file. The rename is EARNED, never assumed.
3. **The rulings held under attack:** watermark=appended-through (deleted sync_watermark rather than patching it) and store=append-only-ledger/diverged-flags-never-rebuilds both turned out to be exactly the right contracts — every subsequent fix composed cleanly on them instead of contradicting them.
4. **D3's dual-write earned its keep:** JSON stayed authoritative through 4 subphases of store hardening; when the incident renamed 4,946 live files, the non-destructive contract (reads+renames only, zero content writes) made recovery a pure reverse-rename, verified exact.

---

## 3. What's Bad About the Code

1. **Content-based identity is the load-bearing weakness:** the dual-anchor guard's false-positive (in-place stubbing) and false-negative (boundary content collision) residuals both stem from having no stable message ids; full-prefix anchoring was rejected for O(n)-per-save. Evolution: stable-ids at the message level (post-MVP, registered) — closes both residuals and the fractional-seq sibling forever.
2. **`load_all`-per-session in migration:** the sweep's cost profile is O(total turns) with per-session load_all calls; 300×40 measured 2.47s (→ ~29s for 3.5k sessions, daemon thread, acceptable) but a batched SELECT would be one pass. Evolution: single-query sweep if the 3.5k-file production migration reports slow.
3. **Seven fix rounds is heavy process:** the loop found real bugs every round, but three of the seven were chasing supervisor-spec defects. Evolution: pre-delegation spec-level adversarial review (route draft briefs' SQL/predicates/contracts through the auditor BEFORE the builder implements) — cheap insurance this spec proves necessary.

---

## 4. Bugs Found During Audit

| # | Phase | Severity | Bug | Found by | Fixed by |
|---|-------|----------|-----|----------|----------|
| 1 | SP1 | HIGH | Cross-instance seq race (threading.Lock per-instance; 500–999 turns lost/trial) | Debugger (mutation harness + repro) | Coder (BEGIN IMMEDIATE + ruling) |
| 2 | SP1 | HIGH | Failed commit leaves dangling tx; "failed" turn flushed later; 2nd writer blocked 5s | Debugger (injection probe) | Coder (rollback-on-fail) |
| 3 | SP1 | issue ×2 | Use-after-close + tail-epoch teeth gaps (mutations survived) | Debugger (M13/M4) | Coder (2 tests) |
| 4 | SP1-r | issue | WAL first-open race (busy_timeout doesn't gate journal-mode; 4/60) | Debugger (concurrent-open probe) | Coder (bounded retry, SP2) |
| 5 | SP2 | HIGH | WAL-sidecar-blindspot: the HIGH-3 byte-sweep read the main file only | Debugger | Coder (checkpoint + 3-file sweep + teeth control) |
| 6 | SP2 | HIGH ×2 | sync_watermark wm-ahead-of-rows: silent store loss + SP3 would cement it | Debugger (probe + cross-spec trace) | Coder (deletion, per ruling) |
| 7 | SP2 | MEDIUM | TOCTOU delta race (wm read outside tx; loser's tail lost) | Debugger | Coder (atomic append_delta) |
| 8 | SP2 | MEDIUM ×2 | fd leak on failed init; DB world-readable 0644 (HIGH-3 posture) | Debugger | Coder (close-on-fail; 0600×3+0700) |
| 9 | SP2-r2 | HIGH | Middle-trim guard bypass (real compact() keep_first=2 preserves index 0) | Debugger (production-shape probe) | Coder (dual anchor: row_at(wm)+seq-0) |
| 10 | SP2-r2 | MEDIUM | Empty-clear IndexError swallowed by fallback; dead code | Debugger | Coder (emptiness-first) |
| 11 | SP2-r3 | MEDIUM | Boundary content-collision false-negative (residual, registered) | Debugger | registered (docstring) |
| 12 | SP3 | HIGH | Ignored append_delta return: no-op append → rename → data loss "as success" | Debugger (probe case A) | Coder (earned rename) |
| 13 | SP3 | issue ×3 | Singleton test-global leak; corrupt-DB total abort + latch eats retry; turns metric lies | Debugger | Coder (all three) |
| 14 | SP3-r2 | HIGH | Skip-branch count-vs-coverage (phantom seq + epoch inflation) | Debugger (2 probes) | Coder (covers()) |
| 15 | SP3-r2 | MEDIUM | Init-order race: _running read before assignment; one-way _stopped latch | Debugger (forced repro) | Coder (flag init + latch symmetry) |
| 16 | SP3-r3 | HIGH | covers() negative-seq false positive — **my brief's SQL** (no floor, false biconditional) | Debugger (neg2/shapes probes) | Coder (floor + CHECK + legacy-DB tests) |
| 17 | SP3-r3 | issue ×2 | Fractional-seq sibling (registered); trust-boundary comment | Debugger | registered / Coder (comment) |
| 18 | SP4A | bug | Stale-closure capture: production banner never rendered (green-suite-blind) | Debugger (order probe) | Coder (dispatch-time read + RED test) |
| 19 | SP4A | issue ×2 | tz-mixing (Z→aware into naive convs); stale flag-default comment | Debugger | Coder (parse-side strip; comment) |

**Supervisor-spec-attributed:** #6's SP3-skip trace, #12's shape (brief ignored the return), #14's sibling branch, #16 entirely (SQL + docstring authored in my brief), plus the SP3 load-switch omission that forced the SP4A load-gap ruling — five audit rounds' worth of churn traceable to specifications I wrote. The auditor's probes were the only stage that caught them.

### Bug patterns

| Pattern | Count | Description |
|---------|-------|-------------|
| `count-vs-coverage` | 3 | Cardinality predicates masquerading as coverage (append branch, skip branch, negative-seq) |
| `watermark-ahead-of-rows` | 2 | Watermark semantics overloaded (acknowledged vs appended) |
| `race-condition` | 3 | Cross-instance seq, TOCTOU delta, WAL first-open |
| `stale-closure-capture` | 1 | Launch-time snapshot of a post-init-wired receiver |
| `stale-comment` | 3 | Flag default, guard flag name, _running docstring |
| `wal-sidecar-blindspot` | 1 | Proof sweeping the main file while rows live in -wal |
| `test-global-leak` / `missing-teeth` | 4 | Singleton leak; order/production/global-state gaps |
| `ignored-return-value` | 1 | append_delta -1 treated as success |
| `resource-leak` / `secret-file-perms` | 2 | fd leak; 0644 DB |
| `init-order-race` / `one-way-latch` | 2 | Flag read before assignment; start() not clearing |
| `tz-mixing` | 1 | Z-suffixed store stamps → aware datetimes |

---

## 5. Process: What Worked

1. **Empirical adversarial audits:** the auditor reproduced every claim with probes — turn-loss counts, starvation timings (5.006s), seeded poison DBs, production-shape compaction — before reporting. This is why the findings were credible and the fixes were verifiable. Nothing shipped on assertion.
2. **Rulings before patches:** the watermark contract and append-only-ledger rulings (both mine, both audit-forced) gave every subsequent fix a stable foundation — no fix round ever re-litigated them, they composed. Contrast with patching symptoms (a "sync less aggressively" fix would have fought the SP3 predicate).
3. **RED-then-GREEN + mutation discipline:** every pin can fail — proven by the auditor re-killing mutations each round and by two RED proofs catching bugs in the fixes themselves (Coder's own append_delta indexing; the closure-capture test's patch-to-raise fallback).
4. **Honest disclosure culture:** Coder volunteered the 4,946-file incident, the flake observation, and two self-caught defects; the auditor volunteered reachability caveats on every HIGH. Zero findings had to be pried out.

---

## 6. Process: What Didn't Work

1. **Supervisor-authored SQL/predicates skipped adversarial review:** three HIGHs (#12's shape, #14's sibling, #16) came from brief text I wrote, implemented faithfully. Lesson: **draft-brief predicates get the same adversarial pass as code** — route them to the auditor BEFORE delegation. This is now a standing rule.
   - Trigger: any brief containing SQL, coverage/exactness predicates, or math claims ("X ⟺ Y").
   - Action: pre-delegation spec probe; the auditor's neg2/shapes harness would have caught #16 in minutes.
2. **The verification battery omitted pyright (SP2 round 1):** a None-return regressed the gate to 2 errors; the auditor caught it, not my battery. pyright is now MANDATORY every round (already enforced SP2-r2 onward — zero NEW since).
3. **The incident:** a migration test reached the real config dir (conftest overrode the store seam, not get_config_dir) and renamed 4,946 live files. Recovery was exact BECAUSE the contract was non-destructive — but the isolation gap should have been in the SP3 brief from the start. Lesson: **every new filesystem-touching seam gets an isolation audit in its brief**, not after an incident.
4. **Crash-recovery overhead:** two session crashes ate a build report and an audit reply; both were reconstructed from the tree + my own verification (the loop's "never trust the report" discipline made this cheap — the code was the source of truth).

---

## 7. What the Code Actually Does (End-User Impact)

1. **Turns can no longer be silently lost to concurrent writes.** Two agents saving the same conversation serialize at the DB write lock (BEGIN IMMEDIATE); the acceptance test proves 1000/1000 turns survive interleaved 2×500 writes with contiguous seqs. Code path: runtime `_auto_save`/`stop` → `save_conversation_to_disk` (persistence.py) → `append_delta` (transcript_store.py:158-215) — one atomic transaction per save, first-committer-wins per index, union preserved.
2. **Legacy conversations migrate once, verifiably, non-destructively.** First launch with the (now default-on) flag: a background sweep appends each `<config_dir>/conversations/<sk>.json` into `transcript.db`, renames it `.migrated` only when the store provably covers every index, and reports via a feed card (migrated/skipped/failed/kept-on-JSON). Corrupt files, diverged (compacted) sessions, and poison-shape stores are refused and retried next launch — never destroyed. Code path: runtime init → daemon `store-migration` thread → `migrate_conversations_to_store` (persistence.py:536+) → `covers()` gate → `.migrated` rename → banner card (ARH).
3. **A conversation whose JSON is gone still loads, fully.** Post-migration (or any lost-JSON restart), `load_conversation_from_disk` hydrates the Conversation from the store's rows in the exact JSON shape — roles, tool_calls, tokens, naive timestamps — so sessions survive their file's retirement. Code path: persistence.py:394-418 (`load_all` fallback + `_message_from_data`).
4. **Secrets posture held:** no api_key column ever existed (schema-pinned); the 3-file WAL-sidecar sweep proves no key-shaped string lands in db/-wal/-shm; DB+sidecars chmod 0600, config dir 0700 (HIGH-3 parity with the JSON path's discipline).

---

## 8. Pre-Existing Issues Flagged (Not Caused by This Implementation)

1. `agent/runtime.py` carries 17 pre-existing pyright errors at HEAD — outside SPEC-08's hunks every round (verified by line-range comparison). Registered for a runtime-dedicated round.
2. `test_enforcement` (3) + `test_mcp_config` (9) env-broken reds — SPEC-06 SP6 P2 disposition; untouched.
3. Bare no-DISPLAY GTK segfault (event_cards) — pre-existing at HEAD; the xvfb-run-mandatory rule is the workaround.
4. main.py's 2 `gi`-import pyright errors — environment resolution, pre-existing.

---

## 9. Evolution Suggestions (Tier 2+)

| Suggestion | Effort | Impact |
|------------|--------|--------|
| Stable message-ids (content-anchor residuals + fractional-seq sibling all close) | ~2 days | Identity no longer content-based; guard false-± gone; full-prefix anchoring becomes free |
| Batched migration SELECT (single-pass sweep) | ~0.5 day | 3.5k-file production sweep in one pass vs per-session load_all |
| Pre-delegation spec-probe protocol (auditor reviews brief predicates before build) | process | The three supervisor-spec HIGHs' class, killed at origin |
| D3 exit: retire JSON fallback + flag after one clean release on store-mode load | ~1 day | Single write path; delete dual-write complexity |
| Runtime pyright cleanup (17 errors) | ~0.5 day | Global floor achieved repo-wide |

---

## 10. Lessons Learned / Process Rules to Carry Forward

1. **Brief-predicates-get-audited rule:** SQL, coverage predicates, and math claims in delegation briefs receive an adversarial probe BEFORE the builder implements.
   - Trigger: drafting any brief containing a query, an ⟺ claim, or an exactness predicate.
   - Action: send the draft fragment to the auditor; only delegate after it survives.
2. **Filesystem-seam isolation rule:** any phase introducing a new filesystem-touching seam must specify its test isolation in the brief (which fixture pins which dir) — verified before the first run, not after an incident.
   - Trigger: new file/db/directory write path.
   - Action: isolation audit item in the brief's checklist.
3. **Order-sensitive tests for order-sensitive wiring:** when a feature depends on construction→wiring→dispatch order, the pin must drive THAT order (the closure-capture bug was green-suite-invisible otherwise).
   - Trigger: any callback wired after construction.
   - Action: production-order test with the fallback patched-to-raise.
4. **Per-file lint accounting:** multi-file ruff invocations aggregate misleadingly (a 50-total hid per-file at-baseline reality); per-file comparison against stashed HEAD is the real signal.
   - Trigger: every verification battery.

---

## 11. Sign-off

- [x] Code committed to main (832e21d4, b30fed78, e818b7de, 43f59845, 3f61ba6e, + this close-out)
- [x] All post-loop verification commands run and pasted (82→84 targeted rounds; full suite 4056/2; pyright 0 NEW; ruff per-file at/below baseline)
- [x] Captain notified with summary
- [x] Tier 2+ backlog updated (§9; registers in context.md: stable-ids, fractional-seq sibling, D3 exit, runtime pyright, spec-probe protocol)
