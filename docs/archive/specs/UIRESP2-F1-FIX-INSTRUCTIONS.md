# F1 Fix — Approval-Pin Spec Amendment: Implementation Instructions

**Work unit:** UIRESP2 F1 (external verification `docs/specs/UIRESP2-PHASE1-3-VERIFICATION-QRUSHER.md` finding 1, HIGH — spec defect accepted)
**Spec:** `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` §2.3.2 — **AMENDED** (read the F1 amendment paragraph in full; it is your contract)
**Baseline:** `38e17ae`. PM rulings: **deletion confirmed** (no archive — 1.B), **build now** (2.A).
**Discipline:** `prompts/steelFramedCodeWriter.md`. Red-first. COMPLETENESS checklist mandatory.

## The defect (verified twice — Qrusher + supervisor)

`_is_pinned_card` pins on the *historical presence* of `needs_approval`/`needs_review`, but `needs_approval` is a creation-time transient that is never cleared. Every exec-approval card ever created is pinned forever. Live feed: 3,081 `needs_approval` cards, 1,884 with `accepted is None` → 2,658 pinned outside the window → effective retention **4,658, not 2,000**, growing ~350/day. Phase 3's "bounded" claim fails at the floor.

Durable decision record exists in `metadata.status` (`approve_exec` writes `"approved"`/`"denied"`, which persists via the update payload) — the pin rule just never reads it.

## Edits

**E1 — `utils/feed_store.py` `_is_pinned_card`:** implement the amended rule. Pinned iff:
- `card.accepted is not None`, OR
- `card.card_type == "git_commit"`, OR
- (`metadata.get("needs_review")` or `metadata.get("needs_approval")`) AND `card.accepted is None` AND `metadata.get("status") not in ("approved", "denied")`

**E2 — `ui/handlers/feed_handler.py` `update_card` (the Phase-1 Edit A block, ~:1026-1032):** the enqueue payload gains `"accepted": card_data.accepted` **only when `card_data.accepted is not None`** (build the payload dict conditionally). This closes the accepted-never-persisted gap for future decisions. Do NOT change the accept/reject paths (:1672/:1733) — they already enqueue `{"accepted": True/False}` directly.

**E3 — `_UPDATABLE_FIELDS`** (`utils/feed_store.py`): already contains `accepted` — verify, no change expected.

**E4 — ARCHITECTURE.md §3.22d** (the pin-rules line): update to the amended wording (genuinely-undecided semantics; one sentence).

## Tests (RED FIRST)

**`tests/test_feed_store.py::TestWindowPruning`** (extend the existing class):
1. `test_resolved_approval_cards_not_pinned` — outside-window cards with `needs_approval=True` + `metadata={"status": "approved"}` (and separately `"denied"`) are NOT pinned → pruned. RED against current code (pinned today). **[Supervisor correction 2026-09-12 — the original bullet also listed `accepted=True` here, which was WRONG: §2.3.2 rule 1 pins `accepted is not None` unconditionally, so an `accepted=True` card survives; the Coder implemented the spec and flagged this instruction error.]**
2. `test_undecided_approval_cards_still_pinned` — outside-window `needs_approval=True`, `accepted is None`, no status → still pinned (guards against over-pruning).
3. `test_genuinely_undecided_semantics_live_fidelity` — build a fixture mirroring the live distribution (e.g. 100 oldest = mix: 40 resolved `needs_approval` w/ status, 30 undecided `needs_approval`, 20 plain, 10 git_commit) → compact at small window → assert exactly the undecided + git_commit among the old are retained; resolved + plain pruned.

**`tests/test_feed_handler.py`** (extend `TestBackgroundPersistWriter`):
4. `test_update_card_persists_accepted_when_decided` — card with `accepted=True`, call `update_card`, drain the queue (stub writer per the existing pattern), assert the enqueued payload contains `"accepted": True`. RED today (payload lacks the key).
5. `test_update_card_payload_omits_accepted_when_none` — `accepted=None` card → payload has NO `accepted` key (no regression to writing None).

**Existing tests that may legitimately change:** none expected — `test_pins_survive_pruning_beyond_window` uses `accepted=True` / `needs_review`-without-status cards (all still pinned under the amended rule). If any existing test used `needs_approval` + decided status expecting a pin, that test encoded the bug — flag it, do not silently change.

## Verification (paste full output)

```bash
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_feed_handler.py -q
# expected: all green incl. the 5 new tests
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_handler.py::TestWindowCompaction tests/test_feed_handler.py::TestSeqNumHandler -q   # no flakes
PYTHONPATH=/tmp/pf-venv2 python3 -m pyflakes utils/feed_store.py ui/handlers/feed_handler.py | grep -c "undefined name"   # 0
```

**Impact proof (paste output):** run this against a COPY of the live feed in /tmp (never the real file) and paste the before/after retention numbers:
```python
# compact_feed(copy, window=2000) → retained count + pinned count under the NEW rule
```
Expected: pinned-outside-window drops from ~2,658 to roughly the undecided-only set (~600-900 of today's snapshot); retention approaches 2,000 + undecided.

## Report

COMPLETENESS per edit + red evidence per test + full outputs + the impact proof. Spec drift flags. STOP for audit.
