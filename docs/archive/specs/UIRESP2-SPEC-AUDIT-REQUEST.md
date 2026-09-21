# SPEC RE-AUDIT REQUEST — SPEC-UI-RESPONSIVENESS-2-PHASES-1-3 (rev 6)

**To:** Debugger
**From:** Supervisor
**Date:** 2026-09-11
**Mode:** `prompts/adversarialDebugger.md`, all 11 sections
**Scope:** re-audit of `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` rev 6 — round-5 fixes

## Exit criterion (unchanged)

Zero new CRITICAL/HIGH = spec ACCEPTED, implementation begins. MEDIUM/LOW route to the Coder as build-time notes. One more round only if a new CRITICAL/HIGH appears.

## Round-5 disposition (verify)

- r5#1 → `import os` added to §2.1.1's import note (verified the module lacks it).
- r5#2/#3/#19 → **structural**: `_requeue_deferred` DELETED. The drain now runs three sequential phases per pass: (1) compactions snapshot, (2) deferred attempted IN PLACE, (3) queue entries. Deferred payloads never move to the queue; tries increments directly; no payload comparison anywhere. Fresh budget by construction: enqueue pops any deferred entry for the key ⇒ queue and deferred entries for one key are mutually exclusive ⇒ a queue entry's first failure is always tries=1.
- r5#18 → compactions snapshot+clear at pass start; during-pass additions (internal retries, external `_enqueue_compaction`) wait for the next pass. Internal re-enqueue is only-if-absent so an external trigger that landed mid-pass keeps its fresh entry.
- r5#4 → `queued_at_stop` snapshot before the join; three-way exit log (straggler WARNING / undrained ERROR / exit-not-observed WARNING).
- r5#15 → `kind`/`task` init removed entirely — the restructure gives each phase a fixed task shape, no mixed-type loop.
- r5#13 → `_ensure_persist_writer` docstring says per-new-thread-start.
- r5#16 → `list(self._project_paths.values())` snapshot.
- r5#5–#12, #14, #17, #20–#24 — your no-bug conclusions; noted the audit-log bundling arithmetic fuzz — dispositions complete.

## Probe list (the changed code only — fresh eyes)

1. **The three-phase drain** (§2.1.2 rewritten): trace each phase. Phase 2 (deferred in place): snapshot `list(items())` → attempt each → on success delete only if `cur[0] is payload` (identity-guard vs a newer failure replacing it — can a newer failure even exist for a key we're iterating? enqueue pops deferred; failure writes deferred; the ONLY concurrent deferred-writer is a queue-entry failure for the same key — can a queue entry for the same key exist while a deferred entry does? Enqueue pops deferred BEFORE adding to queue, so no. But the failure path of phase 3 writes deferred while we iterate phase 2's snapshot — different keys or same? Same key impossible (mutual exclusivity). Confirm or break it.)
2. **Phase ordering**: deferred attempted BEFORE queue — a deferred entry (older payload) is retried before a newer queue entry for the same card… wait, mutual exclusivity means a key can't be in both. But a deferred entry's payload is OLDER than a queue entry that arrives after phase 2 started: enqueue pops the deferred entry (it's mid-iteration on the snapshot list!) — the snapshot already captured it; the attempt proceeds with the stale payload; success deletes nothing (entry already popped); the queue's fresh entry is processed in phase 3. Trace: is the stale attempt harmless (update_feed_card is idempotent last-write-wins; the queue entry's write lands after) — confirm ordering is final-state-correct.
3. **Shutdown three-way log**: `queued_at_stop` counted BEFORE join; a straggler raises leftover above it (WARNING); genuine undrained raises it at or below (ERROR if >0). The `leftover > queued_at_stop` condition can only be stragglers… unless entries were drained AND stragglers added such that leftover < queued_at_stop but > 0 — then the undrained-ERROR fires for what are actually stragglers. Is the message acceptable (entries are present, writer stopped — ERROR is fair)? Confirm honesty.
4. **Compaction snapshot semantics**: `clear()` under lock + iterate the local copy — an external enqueue during iteration appends to the now-empty live list (processed next pass). The only-if-absent internal re-enqueue: after clear, live list may hold the external entry; skip. Confirm no lost compact (any failure either re-enqueues or drops-with-ERROR — no silent loss).
5. **_persist_loop drained-check**: now checks `not self._persist_compactions` — but phase 1 of the drain CLEARS the live list at pass start; entries added after the snapshot sit in the live list (non-empty) → the stop-check sees non-drained → one more pass. Bounded? (Each pass processes ≥1 entry or the list was empty; entries are finite without new enqueues — during shutdown, enqueues are stragglers logged WARNING. A pathological straggler stream during shutdown = unbounded loop? The stop-set is one-way (only `_ensure_persist_writer` clears it, and that requires an enqueue — which is exactly the straggler stream). Trace whether a continuous straggler stream during shutdown can keep the writer alive indefinitely — and whether that's actually wrong (the app is closing; the close-request path calls shutdown once; project-close once; stragglers after that are enqueues from other teardown callbacks — bounded in practice? Document or bound it.)
6. **Audit-log arithmetic** for r5: 5 flagged (4C+1H) + 4 mediums folded (#13/#15/#16 + log wording) — the log's last bullet lists the no-bugs. Verify coverage of every numbered r5 BUG.
7. **Narrative re-sync**: invariants 2/4/5, AC rows, §7 edge cases, the §2.1.2 docstring — all describe the three-phase in-place shape. Grep for stale wording: "re-merge", "requeue", "in-pass", "identity compare", "per-payload budget" (now "by construction"), "_retried_this_pass".
8. Anything else the 11 sections surface.

## Deliverable

BUG #[N] format, severity-tagged, citations, traced where possible. Explicit "no bugs found" per area. **State whether the exit criterion is met.** Report only.
