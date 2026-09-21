# REVIEW-PERSIST-1 — Instructions (Coder)

**Goal:** Close the two decision-persistence gaps found by Debugger's F1 audit
and your own Step-6.6 flags. Decisions currently vanish on reload in specific
paths.

Supervisor verification (2026-09-12, tree at 193ed64) — confirm by reading,
these are the anchors:

- `ui/handlers/feed_handler.py:1911-1915` — `handle_accept`'s **non-git else
  branch** sets `card.accepted = True` + `_update_card_visual(...)` but has
  **NO `_enqueue_card_update`** → decision lost on reload. (The git path at
  :1863/:1866 DOES enqueue correctly.)
- Same class in `handle_reject` (:1916+) — find its non-git path and fix
  symmetrically.
- `_auto_approve_exec_card` (:2360) — same class reported by you earlier;
  verify and fix.
- `agent_runtime_handler.py:1602` sets `card.metadata["needs_review"] = True`
  — grep shows **no site ever clears it / writes the resolution** back to the
  original card after the review bar acts. Debugger's F1-audit BUG #1
  (`ui/handlers/review_handler.py:261-413`: `accept_changes`/`reject_changes`
  never write `accepted`/`status` on the original card).

## EDITS

**Edit A — non-git accept/reject persist parity (`feed_handler.py`).**
In each path that mutates the card's local `accepted`/`status` and calls
`_update_card_visual`, ALSO enqueue the durable write, exactly as the git path
does:
```python
self._enqueue_card_update(project_path, card_id, {"accepted": True})
```
Respect the F1 amendment (spec §2.3.2, pinned by committed tests
test_update_card_persists_accepted_when_decided +
test_rebuild_path_*_when_decided): include `accepted` ONLY when a decision
exists; never write `None`. If `project_path` is legitimately absent, log a
warning rather than silently dropping the decision (silent-loss is the bug
class we're closing).

**Edit B — review-bar resolution writes back to the original card
(`review_handler.py:261-413`).** When `accept_changes`/`reject_changes`
resolves a review, mirror the durable-record write that `approve_exec` already
performs: set the original card's `accepted`/resolution status in memory, clear
`metadata["needs_review"]`, update the widget, and enqueue the persist (same
seam as Edit A). Read `approve_exec` first and mirror its shape — do not invent
a new mechanism. If the review bar has no handle to the feed handler in some
construction path, FLAG it rather than adding a new coupling (handler→handler
imports are forbidden by ARCHITECTURE.md).

**Edit C — tests (RED-FIRST).** Per Debugger's suggested name plus the
non-git parity cases:
1. `test_review_bar_resolution_persists_decision` — review bar accept → the
   ORIGINAL card has the decision persisted (assert the enqueue payload).
2. `test_review_bar_reject_clears_needs_review` — reject → `needs_review`
   gone from metadata AND persist enqueued.
3. `test_non_git_accept_persists_accepted` — card with no project path /
   git-less path → assert the enqueue happens (RED today: no enqueue).
4. `test_non_git_reject_persists_accepted_false` — symmetric.
5. `test_accept_omits_accepted_when_undecided` — guard that we never write
   `None` (F1 parity, branch-independent).
Each must be RED on current code. Paste the failing output.

## GATES

- Red-first evidence for all 5 tests.
- Suites: test_feed_handler, test_review_handler_feed_card,
  test_review_handler (whatever exists), test_feed_card — green under
  `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a`.
- pyflakes /tmp/pf-venv3: 0 undefined on touched files.
- Hermetic: tmp_path; `_no_writer`-style suppression of the persist thread
  (pattern exists in tests/test_feed_handler.py) so drains are deterministic.
- Full suite: baseline is now **14F/3611P** (the 5 documented non-goals must
  not move). Report the new count + list.
- One commit: `fix(feed): persist review decisions on non-git + review-bar paths (F1 audit follow-up)`.
- Flag (don't fix) anything adjacent you find.

Then STOP — audit next.
