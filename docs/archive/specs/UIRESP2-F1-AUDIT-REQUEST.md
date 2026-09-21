# F1 FIX — AUDIT REQUEST

**To:** Debugger — invoke `/home/q/projects/crabcakes/.crabcakes/prompts/adversarialDebugger.md` fresh, all 11 sections.
**Spec contract:** `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` §2.3.2 **F1 amendment** (the genuinely-undecided paragraph — read it; it supersedes the pre-amendment pin text).
**Scope:** `utils/feed_store.py` `_is_pinned_card` :592-617 (rewritten pin rule); `ui/handlers/feed_handler.py` :1046-1057 (conditional accepted-in-payload); `docs/ARCHITECTURE.md` :3048 + the two in-file docstring syncs; 5 new tests (3 store + 2 handler). Baseline `38e17ae` → current tree.

## Context

F1 (external verification, Qrusher): the old pin rule treated never-cleared transient `needs_approval` as permanent → every exec-approval card pinned forever (live: retention 4,718 not 2,000). The durable decision record lives in `metadata.status` (written by `approve_exec`, persisted) — the amended rule reads it. Coder's impact proof on a live copy: **retention 4,718 → 3,003** (= window 2,000 + 1,003 genuine undecided + git_commit). PM ruled: no archive (1.B), build now (2.A).

## Supervisor pre-findings (verify or break)

1. **Coder's spec-error catch is correct:** my instruction's test-1 bullet wrongly listed `accepted=True` among "NOT pinned → pruned". §2.3.2 rule 1 pins `accepted is not None` unconditionally — the Coder implemented the SPEC (correct) and flagged the instruction (my error). Confirm his test asserts `ra-accepted` survives.
2. **The omit-when-None rationale:** if `accepted=None` were written into the journal, `_apply_overlay` would `setattr(accepted, None)` on replay, clobbering a recorded decision back to pending when a body-refresh update replays after a decision update. The conditional payload prevents this. Trace the coalescing-merge interplay (`dict.update` merges only present keys; deferred entries popped fresh) — the Coder verified omitting cannot drop a queued `accepted`; confirm independently.
3. **`status not in ("approved","denied")` vs other statuses:** the card metadata carries other `status` values (e.g. `"pending_approval"`, `"complete"`, `"error"`). Only approved/denied count as decided. Probe: a card with `status="pending_approval"` and `accepted=None` → pinned (correct — genuinely undecided). A card with `needs_review=True`, `status="complete"`, `accepted=None` → pinned (is that right? needs_review cards' review resolution path — does it write accepted or status? If neither, needs_review cards pin forever, recreating F1 for the review flag). **Trace `handle_review` / the review-resolution path and rule on this.**

## Also probe

- The rule's evaluation order (accepted → git_commit → flags): any card that flips category between compactions?
- Red-first evidence soundness (3 RED at `38e17ae` — the instruction bullet error means the Coder's test 1 differs from my instruction's letter; that's the correct call).
- The impact proof's arithmetic (11,486 = 2,000 + 8,483 pruned + 1,003 pinned).
- The 2 docstring syncs (no doc-lies remain in feed_store's module header / compact_feed docstring).
- ARCHITECTURE.md :3048 wording vs the amended rule.

## Deliverable

BUG #[N] format, severity-tagged, traced. Explicit "no bugs found" per area. **Verdict: ACCEPT / SEND-BACK.**
