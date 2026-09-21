# Phase 2 of 5 — Persistent Decision Badges on ALL Card Types

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-FEED-CARD-UX.md` (read it in full, especially the "Spec Revision History" at the top and Section 2.4)

**Phase 1 status:** COMPLETE (committed working tree, 1694 passed, 1 skipped, 4 warnings). The model has `is_actionable()` and `is_informational()` static methods, the view uses them for button visibility, and sub-state CSS for `agent_action` cards is in place.

**Scope of this phase:** Section 2.4 of the spec (2 files). Small but high-impact fix.

---

## ⚠️ MANDATORY: Read every file in full before writing any code

Per `prompts/steelFramedCodeWriter.md` Rule 1, before writing ANY code, read EVERY file you will touch. ALL of it. Not snippets — the whole file. The spec's line numbers are stale (file has grown 200+ lines since the spec was drafted) — anchor edits to identifiers (function names, class names, dataclass field names), NOT line numbers.

**Files to read in full before writing any code:**

1. `ui/handlers/feed_handler.py` (977 lines) — find `_add_git_card()`, `handle_accept()`, `handle_reject()`, `handle_approve_exec()`. Understand the data flow: original_card → git_card → add_card.
2. `ui/handlers/agent_runtime_handler.py` (996 lines) — find `approve_exec()`. Note the structure: `if self._fh is not None:` block at the end of the function that updates the card.
3. `tests/test_feed_handler.py` (366 lines) — existing test patterns to follow
4. `docs/ARCHITECTURE.md` Section 3.22c (read the FeedHandler public API)

**Output a discovery block before writing code** (per steelFramedCodeWriter Step 0).

---

## The bug

**User complaint #1:** "Accept/Reject buttons disappear after clicking — no persistent visual record of the decision remains on all card types."

Two card-creation paths are missing the `card.accepted` field:

1. **git_commit cards** (created by `feed_handler._add_git_card()`): The function builds a new `FeedCardData(card_type="git_commit", ...)` and never sets `accepted`. So the git_commit card never gets the ACCEPTED/REJECTED badge.

2. **Approval cards** (created by `agent_runtime_handler.approve_exec()`): After the user clicks Approve/Deny, the function sets `card.metadata["status"] = "approved" or "denied"` and calls `self._fh.update_card(approval_id, card)`. But it never sets `card.accepted`. So after approval/denial, the card still appears as a pending approval card (no badge, buttons still visible).

Both paths feed back into `build_feed_card()` which uses `card.accepted is not None` (`is_resolved = True`) to determine button visibility, and `_update_card_visual()` which uses `card.accepted` to set the ACCEPTED/REJECTED CSS class and badge.

---

## Edits

### Edit 1: `ui/handlers/feed_handler.py` — Set `accepted=accepted` in `_add_git_card()`

Find `_add_git_card()` (search for the function name). The current code builds a `git_card` with `card_type="git_commit"` and many other fields. Add `accepted=accepted` to the `FeedCardData(...)` constructor call.

**Use the EXACT code from spec section 2.4** which shows the new line. The key addition is one parameter: `accepted=accepted,` in the `FeedCardData` constructor.

**Before:**
```python
git_card = FeedCardData(
    card_type="git_commit",
    source="git",
    title=f"{action}: {original_card.title}",
    body=result.stdout.strip() if result.stdout else "",
    author="PM",
    timestamp=datetime.now(timezone.utc),
    project_name=original_card.project_name,
    commit_sha=result.sha if hasattr(result, 'sha') and result.sha else None,
    file_path=original_card.file_path,
)
```

**After:**
```python
git_card = FeedCardData(
    card_type="git_commit",
    source="git",
    title=f"{action}: {original_card.title}",
    body=result.stdout.strip() if result.stdout else "",
    author="PM",
    timestamp=datetime.now(timezone.utc),
    project_name=original_card.project_name,
    commit_sha=result.sha if hasattr(result, 'sha') and result.sha else None,
    file_path=original_card.file_path,
    accepted=accepted,  # NEW — propagate decision so badge renders
)
```

The local variable `accepted` is already computed earlier in the function (search for `accepted = original_card.accepted is True`). The new line uses that local variable.

### Edit 2: `ui/handlers/agent_runtime_handler.py` — Set `card.accepted = approved` in `approve_exec()`

Find `approve_exec()` (search for the function name). The function structure is:

1. Pop pending approval from `self._pending_approvals`
2. Forward approval to runtime (for loop over `self._runtimes.items()`)
3. Update the card in the feed: `if self._fh is not None:` block that does:
   - `card = self._fh.get_card(approval_id)` (with `if card is not None:` guard)
   - `card.metadata["status"] = "approved" if approved else "denied"`
   - `self._fh.update_card(approval_id, card)`

**Add `card.accepted = approved` between the metadata update and the update_card call** (inside the `if self._fh is not None:` block AND inside the `if card is not None:` guard):

**Before:**
```python
        if self._fh is not None:
            card = self._fh.get_card(approval_id)
            if card is not None:
                card.metadata["status"] = "approved" if approved else "denied"
                self._fh.update_card(approval_id, card)
```

**After:**
```python
        if self._fh is not None:
            card = self._fh.get_card(approval_id)
            if card is not None:
                card.metadata["status"] = "approved" if approved else "denied"
                card.accepted = approved  # NEW: propagate decision so badge renders (Phase 2)
                self._fh.update_card(approval_id, card)
```

**Why inside the `if card is not None:` guard:** the existing code already guards against missing cards. The new line goes inside that guard so it doesn't crash on a missing card.

**Why after the metadata update, before update_card:** `update_card()` rebuilds the widget via `build_feed_card()` which reads `card.accepted` to decide button visibility. Setting `card.accepted` before `update_card` is required for the badge to render and buttons to hide.

### Edit 3: `tests/test_feed_handler.py` — Add Phase 2 tests

Add the `TestPersistentBadges` test class from spec section 9 (Phase 2 Tests). The tests verify:
- After accepting a file-change card, the git_commit card created has `accepted=True`
- After approving a pending approval card, the card's `accepted` field is `True`
- After denying a pending approval card, the card's `accepted` field is `False`

**Use the EXACT test code from spec section 9** with these adjustments for the actual codebase:
- The actual `FeedCardData` constructor takes `card_type, source, title, body, author, timestamp, project_name, ...` (positional or keyword)
- Use `datetime.now(timezone.utc)` for the timestamp
- For the approval test, mock `self._fh` so `get_card()` returns a known card
- For the git_commit test, the function is `_add_git_card(self, original_card, result)` — pass a mock result with `.success=True, .stdout="...", .sha="abc123"`

**Total: 3 edits in 2 source files + 1 test file. ~5 lines of production code + ~50-80 lines of test code.**

---

## Rules

- Use the `prompts/steelFramedCodeWriter.md` prompt
- Read every file in full before editing
- Anchor edits to identifiers, NOT line numbers (the spec's line numbers are stale)
- Scope is exactly the 3 edits above. Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue, note it in the COMPLETENESS checklist as "Related issue found, not fixed in this phase" and stop.
- Do NOT touch the ARCHITECTURE.md doc updates — those come after all phases ship.
- Do NOT touch Phase 3-5 work (sequence numbers, smart scroll, batch accept). Strictly Phase 2 only.
- Do NOT touch the Phase 1 work. The static methods and button logic from Phase 1 are already in place and tested.

## Verification commands to run (in order)

1. **`_add_git_card` sets `accepted`:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "accepted=accepted" ui/handlers/feed_handler.py
   ```
   Expect: ≥ 1 match inside `_add_git_card`

2. **`approve_exec` sets `card.accepted`:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "card.accepted = approved" ui/handlers/agent_runtime_handler.py
   ```
   Expect: 1 match inside the `if self._fh is not None:` block

3. **New tests pass:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest tests/test_feed_handler.py -v
   ```
   Expect: all existing tests pass + new TestPersistentBadges tests pass

4. **Import sanity:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "import ui.handlers.feed_handler; import ui.handlers.agent_runtime_handler; print('OK')"
   ```
   Expect: `OK`

5. **No accidental scope creep:**
   ```bash
   cd /path/to/projects/crabcakes && git diff HEAD --stat
   ```
   Expect: only `ui/handlers/feed_handler.py`, `ui/handlers/agent_runtime_handler.py`, `tests/test_feed_handler.py` changed (plus any unrelated changes from previous sessions that are not from this phase). Phase 1 changes should still be present but not modified.

6. **Full test suite (sanity — should be no new failures):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest -x -q
   ```
   Expect: ≥ 1694 passed (Phase 1 + Phase 2 new tests), 1 skipped, 4 warnings

## Report

When done, send back a completion report with:
- Files changed with line numbers (of the actual edits, not the spec's claimed line numbers)
- Output of all 6 verification commands
- Full pytest output for the test_feed_handler.py run
- COMPLETENESS checklist (per steelFramedCodeWriter Step 6.5)
- Any related issues found (flagged, not silently fixed)

**Required word marker for /ask acknowledgment: "please write"** — include it in your response so the channel knows your acknowledgment is canonical.

**Do not skip the COMPLETENESS checklist.** Include every edit with `[x]` or `[NOT DONE] WHY` and paste the evidence. A response without the literal `**COMPLETENESS:** [x]` block is a missing deliverable.
