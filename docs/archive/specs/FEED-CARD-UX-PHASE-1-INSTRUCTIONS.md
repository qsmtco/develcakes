# Phase 1 of 5 — Card Type Button Policy + Color Palette

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-FEED-CARD-UX.md` (1216 lines — read it in full before starting, especially the "Spec Revision History" at the top and the relevant section 2 sub-sections below)

**Scope of this phase:** Section 2.1 + 2.2 + 2.3 of the spec (3 files). Foundation work that all other phases depend on.

---

## ⚠️ MANDATORY: Read every file in full before writing any code

Per `prompts/steelFramedCodeWriter.md` Rule 1, before writing ANY code, read EVERY file you will touch. ALL of it. Not snippets — the whole file. The spec's line numbers are stale (file has grown 200+ lines since the spec was drafted) — anchor edits to identifiers (function names, class names, dataclass field names), NOT line numbers.

**Files to read in full before writing any code:**

1. `models/feed_card.py` (161 lines) — current dataclass, `css_class_for_type`, helpers
2. `ui/views/feed_card.py` (581 lines) — `build_feed_card` factory, button logic, `update_card_badge`
3. `ui/styles.py` (1185 lines) — find the existing feed-card CSS block (search for `.feed-card-`), confirm hardcoded-hex palette (no `@define-color` / `var(--name)` variables)
4. `tests/test_feed_card.py` (205 lines) — existing test patterns to follow
5. `docs/ARCHITECTURE.md` Section 3.22a, 3.22b (read these so you understand the current public API)

**Output a discovery block before writing code** (per steelFramedCodeWriter Step 0).

---

## Edits

### Edit 1: `models/feed_card.py` — Add `is_actionable()` and `is_informational()` static methods

Add the two new static helper methods to the `FeedCardData` class. Use the **EXACT code from spec section 2.1**, which already includes the Issue 5 fix (`None` in the `status in` check).

**Method signatures verified against actual class** (anchor to the class name `FeedCardData`, not a line number):

```python
@staticmethod
def is_actionable(card_type: CardType, metadata: dict | None = None) -> bool:
    """True if this card type requires user action (Accept/Reject/Approve/Deny)."""
    # Approval-request cards (exec_command needing approval)
    if metadata and metadata.get("needs_approval"):
        return True
    # File-change cards that have git backing (can be accepted/rejected)
    if card_type in ("diff", "file_created", "file_modified", "file_deleted"):
        return True
    return False

@staticmethod
def is_informational(card_type: CardType, metadata: dict | None = None) -> bool:
    """True if this card is purely informational — no buttons needed."""
    # Approval cards are actionable, not informational
    if metadata and metadata.get("needs_approval"):
        return False
    # git_commit: result of accept/reject — informational
    if card_type == "git_commit":
        return True
    # agent_action with status=running/complete/error: tool execution log.
    # Also include status=None: a crabcard-parsed or manually-created
    # agent_action card with no status field is still informational (a
    # tool execution log), not actionable. Without this, the card would
    # show Accept/Reject buttons that do nothing.
    if card_type == "agent_action":
        status = metadata.get("status") if metadata else None
        if status in (None, "running", "complete", "error"):
            return True
    # system events, audit reports, tasks: informational
    if card_type in ("system", "audit_report", "task", "dir_created", "dir_deleted"):
        return True
    return False
```

**Imports required:** None new — uses only `CardType` and `dict` which are already in scope.

### Edit 2: `ui/views/feed_card.py` — Replace button visibility logic in `build_feed_card()`

Find the existing button logic in `build_feed_card()`. The current code uses a binary `is_commit` check (`card_data.card_type == "git_commit"`) to skip buttons. Replace it with the new logic that uses `FeedCardData.is_actionable()` / `FeedCardData.is_informational()`.

**Use the EXACT code from spec section 2.2.** The new code:
- Shows full action row (Review + Accept/Reject or Approve/Deny) when `is_actionable and not is_resolved`
- Shows Review only when `is_actionable and is_resolved`
- Shows NO buttons when `is_informational`
- Uses "Approve"/"Deny" labels for approval cards, "Accept"/"Reject" for others (read `card_data.metadata.get("needs_approval")` to choose)

**Important:** The current code also has the "ACCEPTED/REJECTED badge" logic which is in scope for Phase 1 (the badge is added when `card_data.accepted is not None`). Do NOT remove or change the badge logic — only change the button logic.

### Edit 3: `ui/views/feed_card.py` — Add sub-state CSS class application

Add sub-state CSS classes for `agent_action` cards based on metadata, AFTER the existing `css_class_for_type` class is applied. Use this code from spec section 2.3:

```python
    # Sub-state CSS classes for agent_action cards
    if card_data.card_type == "agent_action":
        if card_data.metadata.get("needs_approval"):
            card.add_css_class("feed-card-approval")
        elif card_data.metadata.get("status") == "running":
            card.add_css_class("feed-card-running")
        elif card_data.metadata.get("status") == "complete":
            card.add_css_class("feed-card-complete")
        elif card_data.metadata.get("status") == "error":
            card.add_css_class("feed-card-error")
```

Find the appropriate insertion point by searching for `css_class_for_type` in `build_feed_card()` — the new code goes RIGHT AFTER the existing `card.add_css_class(...)` call.

### Edit 4: `ui/styles.py` — Add new CSS classes

Append the new CSS to the `APP_CSS` string in `ui/styles.py`. Use the EXACT code from spec section 2.3:

```css
/* Agent action sub-states */
.feed-card-agent.feed-card-approval .feed-card-header {
    background: #5a3d2d; color: #ffb085;
}
.feed-card-agent.feed-card-approval .feed-card-body {
    background: #3d2a1a;
}
.feed-card-agent.feed-card-running .feed-card-header {
    background: #2d3a5a; color: #a8c1e6;
}
.feed-card-agent.feed-card-running .feed-card-body {
    background: #1a273d;
}
.feed-card-agent.feed-card-complete .feed-card-header {
    background: #2d4a3d; color: #a8e6c1;
}
.feed-card-agent.feed-card-complete .feed-card-body {
    background: #1a3d2a;
}
.feed-card-agent.feed-card-error .feed-card-header {
    background: #5a2d2d; color: #e6a8a8;
}
.feed-card-agent.feed-card-error .feed-card-body {
    background: #3d1a1a;
}
```

**Find the insertion point** by searching for `.feed-card-audit` in `ui/styles.py` — append the new CSS AFTER that block.

**CSS color note:** The new sub-state colors (orange/blue/green/red) are NEW colors not in the existing palette. The existing palette uses `#6366f1` (indigo), `#34, 197, 94` (green), `#ef4444` (red). The new colors follow the same hex format. No CSS variables exist in the codebase — hardcoded hex is the convention.

### Edit 5: `tests/test_feed_card.py` — Add the test class from spec section 9

Add the `TestIsActionable`, `TestIsInformational`, and `TestActionableInformationalMutuallyExclusive` test classes from spec section 9 (Phase 1 Tests). Use the EXACT test code from the spec — it covers:
- Every card type is in exactly one of the two categories
- `metadata=None` is handled gracefully
- `needs_approval=True` makes a card actionable
- `agent_action` with no `status` field is informational (the Issue 5 regression test)

**Total: 5 edits in 3 files + 1 test file. ~80 lines of production code + ~150 lines of test code.**

---

## Rules

- Use the `prompts/steelFramedCodeWriter.md` prompt
- Read every file in full before editing
- Anchor edits to identifiers, NOT line numbers (the spec's line numbers are stale)
- Scope is exactly the 5 edits above. Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue, note it in the COMPLETENESS checklist as "Related issue found, not fixed in this phase" and stop.
- Do NOT touch the ARCHITECTURE.md doc updates — those come after all phases ship.
- Do NOT touch Phase 2-5 work (decision badges, seq numbers, smart scroll, batch accept). Strictly Phase 1 only.
- Do NOT refactor existing code. The current `is_commit` check must be REPLACED by the new logic, not augmented.

## Verification commands to run (in order)

1. **Import sanity:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "import models.feed_card; import ui.views.feed_card; import ui.styles; print('OK')"
   ```
   Expect: `OK`

2. **New methods exist with correct signatures:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "
   from models.feed_card import FeedCardData
   import inspect
   print('is_actionable signature:', inspect.signature(FeedCardData.is_actionable))
   print('is_informational signature:', inspect.signature(FeedCardData.is_informational))
   # Spot-check the Issue 5 fix: agent_action with no status
   assert FeedCardData.is_informational('agent_action', {}) is True, 'Issue 5 regression'
   print('Issue 5 regression test: PASS')
   "
   ```
   Expect: signatures printed, `Issue 5 regression test: PASS`

3. **New tests pass:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest tests/test_feed_card.py -v
   ```
   Expect: all existing tests pass + new TestIsActionable + TestIsInformational + TestActionableInformationalMutuallyExclusive tests pass

4. **CSS was appended:**
   ```bash
   cd /path/to/projects/crabcakes && grep -c "feed-card-approval" ui/styles.py
   ```
   Expect: ≥ 1 (definition in CSS block)

5. **No accidental scope creep:**
   ```bash
   cd /path/to/projects/crabcakes && git diff HEAD --stat
   ```
   Expect: only `models/feed_card.py`, `ui/views/feed_card.py`, `ui/styles.py`, `tests/test_feed_card.py` changed. No other files.

6. **Full test suite (sanity — should be no new failures):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest -x -q
   ```
   Expect: 1662 passed, 1 skipped, 4 warnings (or whatever the new baseline is after adding the new tests)

## Report

When done, send back a completion report with:
- Files changed with line numbers (of the actual edits, not the spec's claimed line numbers)
- Output of all 6 verification commands
- Full pytest output for the test_feed_card.py run
- COMPLETENESS checklist (per steelFramedCodeWriter Step 6.5)
- Any related issues found (flagged, not silently fixed)

**Required word marker for /ask acknowledgment: "please write"** — include it in your response so the channel knows your acknowledgment is canonical.

**Do not skip the COMPLETENESS checklist.** Include every edit with `[x]` or `[NOT DONE] WHY` and paste the evidence. A response without the literal `**COMPLETENESS:** [x]` block is a missing deliverable.
