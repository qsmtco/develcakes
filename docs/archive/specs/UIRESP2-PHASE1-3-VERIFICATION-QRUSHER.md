# UIRESP2 PHASES 1–3 — EXTERNAL VERIFICATION REPORT

**From:** Lt. Qrusher (independent verification; not part of the build or audit chain)
**To:** Supervisor — for the Phase 3 final audit and the post-mortem
**Date:** 2026-09-11
**Baseline verified:** `8749ecb` (Phase 3) with `c623196` (Phase 2), `a3c95c2` (Phase 1)
**Method:** read-only inspection + independent execution of the suites and the headline performance claims. No repository files were modified by this verification.

---

## 1. Verdict

**The implementation is faithful to the spec, well-tested, and the headline claim is real.** 246 tests pass (68 `feed_store`/`low12_13` + 178 `feed_handler`). The measured cost that motivated the unit is gone: `append_card_update` is **0.09 ms** against the live 10,900-card feed, versus the ~620 ms synchronous full-file rewrite measured on 2026-09-11.

Three findings follow, ranked. **F1 is a defect in the retention rule itself — the rule originated in my PM-level spec, not in the Coder's work.** I own it; the implementation faithfully implements what it was told.

---

## 2. Verified independently (no action needed — audit should not re-litigate)

| Claim | Result |
|---|---|
| Sendback CRITICAL fixed — one lock inode for all feed_store mutations | ✅ `_acquire_lock(jp)` gone; `:322` now `_acquire_lock(_feed_path(project_path))`; all 6 call sites on the feed path |
| Sendback regression tests present | ✅ `TestSingleLockInode` (`test_journal_append_and_compaction_share_one_lock_inode`, `test_no_update_lost_when_append_interleaves_compaction`) |
| `feed_store` + `low12_13` suites | ✅ 68 passed in 4.98 s |
| `feed_handler` suite (GTK, under `xvfb-run`) | ✅ 178 passed in 1.81 s |
| Journal append is O(1) | ✅ 0.09 ms on a 10,900-card / 17 MB feed (vs 0.11 ms empty — no size dependence) |
| Compaction folds + prunes + truncates | ✅ 0.40 s for 10,899 cards; snapshot 17.0 → 6.8 MB |
| Post-compaction load bound (<100 ms) | ✅ 65.3 ms for 4,531 cards |
| Bounded lock | ✅ `_LOCK_TIMEOUT_SEC = 2.0`; `test_acquire_lock_returns_none_within_bound` |
| Prune surfaced + persisted, incl. the tricky cases | ✅ 6 tests (`persisted-while-loading`, `suppressed-when-project-closed`, `not-misfiled-when-project-switched`) |
| Order preservation, pins, `seq_num` max across compaction | ✅ tested (`test_pins_survive_pruning_beyond_window`, `test_seq_num_max_survives_compaction`) |
| `ARCHITECTURE.md` updated (§3.22c/§3.22d/§4.14) | ✅ |

Also acknowledged: the `_surface_prune_card` self-satisfying-guard bug you found and fixed directly in `8749ecb`, with `test_prune_card_not_misfiled_when_project_switched_mid_compaction` added and §2.3.5 amended. That is exactly the right response to a spec bug.

---

## 3. Findings

### F1 — HIGH — the `needs_approval` pin makes the sliding window unable to bind (spec defect, mine)

**Rule as specified** (§2.3.2 of the build spec, carried verbatim from my PM spec §2.3):

> a card is **pinned** (never pruned) if any of: `accepted is not None`; `metadata.get("needs_review")` or `metadata.get("needs_approval")`; `card_type == "git_commit"`.

**Measured against the live feed (10,924 cards, 17.0 MB):**

| | |
|---|---|
| Cards outside the 2,000-card window | 8,924 |
| Pinned by the current rules | **2,537** |
| Cards the window actually retains | **4,537** (not 2,000) |
| Pinned *specifically* by `needs_approval` | **2,335** (92 % of all pins) |
| …of those, no decision recorded (`accepted is None`), oldest dated 2026-08-21 | **1,634** |

**Why it happens.** `needs_approval` is a *transient* flag — it marks "this card is waiting for an approval". It is never cleared when the approval is resolved. The pin rule treats it as permanent state, so **every exec-approval card ever created is pinned forever.**

Compounding root cause: `handle_approve_exec` (`feed_handler.py:2120`) delegates to `_on_approve_exec` → `AgentRuntimeHandler.approve_exec`, which never writes the decision back onto the card. So `accepted` stays `None`, and the `accepted is not None` pin — which *should* be the durable one — never covers these cards either.

**Growth.** 2,979 `needs_approval` cards already exist; recent creation rate is 250–1,160/day (2026-09-08 alone: 1,161). At ~350/day, the permanently-pinned set grows by ~350 cards/day. **Phase 3 therefore does not bound feed growth** — it converts unbounded growth into linear growth with a large constant. In 30 days the floor is ~13,000 pinned cards and the 17 MB problem is back.

**Fix direction (needs a spec amendment, not just code):**
1. Define "pending" precisely: pin on `needs_review`/`needs_approval` **only while the card is genuinely undecided** (e.g. `accepted is None` **and** an explicit pending marker), not on the historical presence of the flag.
2. Write the decision back to the card when an approval resolves, so the durable `accepted is not None` pin covers it and the transient flag stops mattering.
3. Consider a TTL for undecided cards (an approval nobody answered for 3 weeks is not "actionable" — it is orphaned history).

**Impact check:** with the pin rule corrected (rule 1 alone, modelled on the live feed), pinned-outside-window drops from 2,537 → **901**, and retention from 4,537 → **2,901**.

**Sequencing note:** the first compaction has **not** yet run against the live feed (see F4). Fixing the rule **before** that first run avoids baking a 4,537-card floor — and avoids a "pruned N" surface card whose number is wrong-by-design.

### F2 — MEDIUM — pruning is destructive with no archive

`compact_feed` drops pruned cards permanently. There is no archive path (grep for `archive` across `feed_store.py` / `feed_handler.py`: nothing). The first live compaction will drop **~6,400 cards** — and once F1 is fixed, **~8,000** — leaving nothing behind but a WARNING log line and a count on a surfaced card.

The feed is the oversight record for this project. Dropping ~8,000 historical cards with only a count is a defensible product decision, but it should be a **deliberate** one.

**Recommendation (cheap, matches the journal philosophy):** before dropping, append the pruned cards to `.crabcakes/feed-archive.jsonl` — newline-delimited, append-only, **never loaded by any read path**. Cost is one sequential write during compaction; the record becomes recoverable; the window stays binding. Given Phase 2 already established the append-only sidecar pattern, this is small and consistent.

### F3 — LOW — `seq_floor` not implemented (spec/code mismatch)

My PM spec §2.3 required a `feed-meta.json` `{"seq_floor": N}` so numbering continues after pruning. It is not implemented (`seq_floor`: 0 hits).

**The property still holds**, and their test proves it: the window retains the *newest* cards, which carry the highest `seq_num`, so the recomputed max is correct across compaction (verified: max `seq_num` 79,283 retained after a live compaction on a copy; `test_seq_num_max_survives_compaction` covers it).

**Recommendation:** amend the spec to drop the `seq_floor` requirement (as was done for §2.3.5) rather than add code for a case the window order makes impossible. The audit should not fail the phase on a spec line the design makes moot.

### F4 — INFO — the fix is not live; the running app still exhibits the original defect

The running app (PID 932693) **started 10:24**; the three phase commits landed at **12:15 / 18:35 / 18:53**. `.crabcakes/feed-updates.jsonl` does not exist in the repo, confirming the live process is running pre-Phase-2 code. The live feed grew 15.5 MB → 17.0 MB during today alone.

**Action:** restart the app after the final audit closes to pick up Phases 1–3. Until then the freeze behaviour is unchanged in the UI.

### F5 — INFO — commit hygiene (already self-flagged)

`8749ecb` swept untracked drafts via `git add -A`, including two documents authored by me outside this unit: `docs/specs/SPEC-AGENT-CONTROL-1.md` and `docs/proposals/PROPOSAL-post-responsiveness-priorities.md`. Harmless and on-topic, but the post-mortem's process section should attribute them correctly — they are not artifacts of the UIRESP2 build.

---

## 4. What this verification did NOT cover

Stated plainly so the audit's coverage is not over-read:

- **Full-suite baseline byte-identity** (the 40-failure set vs `352caf7`) — not run here; the GTK suites carry the documented OOM classes and I did not want to add load to a machine running the team. This remains the audit's own gate.
- **pyflakes undefined-name count** — not run.
- **The before/after timing evidence** the spec requires each phase to paste — I verified the *end state* independently (above), not the per-phase evidence trail.
- **Behaviour under concurrent live load** — my tests ran against fixtures and copies, not the live app mid-turn.

---

## 5. Recommended handling

1. **Raise F1 in the current final audit** — as a spec-level defect, not a Coder defect. It changes the phase's *outcome* claim ("the feed is now bounded"), which is the whole point of Phase 3.
2. **Decide F2 before the first live compaction** — archive or accept the loss, but choose deliberately.
3. **Amend the spec for F3** rather than coding around it.
4. **Fix F1 before the first live compaction runs** — sequencing only; no urgency beyond that.
5. **Restart the app** once the audit closes (F4).

Credit where due: the loop's own discipline caught the `_surface_prune_card` guard bug, the split-lock CRITICAL, and the tri-state semantics without me. F1 is the class of defect their process cannot catch — a rule that is implemented exactly as written and is still wrong. That is a spec-review failure, and it sits with the spec author: me.
