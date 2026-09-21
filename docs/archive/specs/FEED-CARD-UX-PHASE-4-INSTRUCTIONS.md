# Phase 4 of 5 — Smart Scroll (Never Auto-Scroll to Top)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-FEED-CARD-UX.md` (read it in full, especially "Spec Revision History" and Section 2.8/2.9)

**Phase 1-3 status:** COMPLETE (button policy + decision badges + sequence numbers all in working tree, 1698+ tests passing)

**Scope of this phase:** Section 2.8 + 2.9 of the spec (2 files). Small, focused fix. This is the user's #2 complaint.

---

## ⚠️ MANDATORY: Read every file in full before writing any code

Per `prompts/steelFramedCodeWriter.md` Rule 1, before writing ANY code, read EVERY file you will touch. ALL of it. Not snippets — the whole file. The spec's line numbers are stale — anchor edits to identifiers, NOT line numbers.

**Files to read in full before writing any code:**

1. `ui/views/feed_tab.py` (167 lines) — find the existing `scroll_to_bottom()` method. The new `smart_scroll_to_bottom()` goes in the same class. Read the `__init__` to understand the `self._feed_scroll` reference.
2. `ui/handlers/feed_handler.py` (977 lines) — find `add_card()` (specifically the `_append` closure that calls `self._feed_tab.scroll_to_bottom()`). Also find `on_project_opened()` which keeps the UNCONDITIONAL `scroll_to_bottom()` call.
3. `tests/test_feed_handler.py` (366 lines) — existing test patterns
4. `docs/ARCHITECTURE.md` Section 3.35 (read the FeedTab public API)

**Output a discovery block before writing code** (per steelFramedCodeWriter Step 0).

---

## The bug

**User complaint #2:** "Feed auto-scrolls to top when new cards arrive instead of staying at the current position or scrolling to the bottom (newest)."

**Root cause:** GTK's adjustment mechanism. When a card is appended to a `Gtk.Box` with `set_valign(Gtk.Align.START)`, the viewport adjustment can temporarily reset. The unconditional `scroll_to_bottom()` follows the adjustment to the bottom, but during the reset the user sees the view jump to the top.

**Fix:** Add a `smart_scroll_to_bottom()` that only scrolls to the bottom when the user is already near the bottom (within 80px). When the user has scrolled up to read old cards, do NOT auto-scroll — preserve their position.

---

## Edits

### Edit 1: `ui/views/feed_tab.py` — Add `smart_scroll_to_bottom()` method

Find the existing `scroll_to_bottom()` method. Keep it unchanged (it's used for unconditional scroll on project open). Add a new method right after it:

```python
    def smart_scroll_to_bottom(self) -> None:
        """
        Only scroll to bottom if the user is already near the bottom (within 80px).
        If the user has scrolled up to read old cards, do NOT auto-scroll —
        preserve their reading position. (Phase 4)

        Distinguishes from scroll_to_bottom() (unconditional) which is used
        on project open where we always want to jump to the newest.
        """
        if self._feed_scroll is None:
            return
        vadj = self._feed_scroll.get_vadjustment()
        if vadj is None:
            return
        current = vadj.get_value()
        upper = vadj.get_upper()
        page_size = vadj.get_page_size()
        distance_from_bottom = upper - page_size - current
        if distance_from_bottom < 80:
            vadj.set_value(upper)
```

**Important:** Do NOT change `card_container.set_vexpand(True)` to `set_vexpand(False)` — `vexpand=True` is needed for the empty-state widget to center vertically when there are no cards. The smart scroll is the only fix; the container's expand behavior stays as-is.

### Edit 2: `ui/handlers/feed_handler.py` — Use smart scroll in `add_card()`

Find the `_append()` closure inside `add_card()` (search for `def _append():` inside `add_card`). It calls `self._feed_tab.scroll_to_bottom()`. Change it to `self._feed_tab.smart_scroll_to_bottom()`:

**Before:**
```python
        def _append():
            if self._feed_tab is not None:
                self._feed_tab.append_card(widget, card_id)
                self._feed_tab.scroll_to_bottom()
```

**After:**
```python
        def _append():
            if self._feed_tab is not None:
                self._feed_tab.append_card(widget, card_id)
                self._feed_tab.smart_scroll_to_bottom()  # Phase 4
```

**Keep the unconditional `scroll_to_bottom()` call in `on_project_opened()`** — when a project opens, we always want to jump to the bottom (the unconditional call there is correct behavior).

### Edit 3: `tests/test_feed_handler.py` — Add Phase 4 tests

Add the `TestSmartScroll` test class from spec section 9 (Phase 4 Tests). The tests cover:
- `smart_scroll_to_bottom()` scrolls when user is within 80px of bottom
- `smart_scroll_to_bottom()` does NOT scroll when user is >80px from bottom
- `scroll_to_bottom()` (unconditional) always scrolls to bottom (even from top)

For testing, the existing `MockFeedTab` may need a vadjustment attribute. Add a fake `Gtk.Adjustment`-like object to the mock with `.get_value()`, `.get_upper()`, `.get_page_size()`, `.set_value()` methods. The tests should set up specific positions and verify the call behavior.

**Total: 3 edits in 2 source files + 1 test file. ~20 lines of production code + ~50-100 lines of test code.**

---

## Rules

- Use the `prompts/steelFramedCodeWriter.md` prompt
- Read every file in full before editing
- Anchor edits to identifiers, NOT line numbers
- Scope is exactly the 3 edits above. Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue, note it in the COMPLETENESS checklist as "Related issue found, not fixed in this phase" and stop.
- Do NOT touch the ARCHITECTURE.md doc updates — those come after all phases ship.
- Do NOT touch Phase 5 work (batch accept + batch bar). Strictly Phase 4 only.
- Do NOT remove the unconditional `scroll_to_bottom()` method — it's still needed on project open.
- Do NOT change `card_container.set_vexpand(True)` to `set_vexpand(False)` (would break empty-state centering).

## Verification commands to run (in order)

1. **New method exists:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "def smart_scroll_to_bottom" ui/views/feed_tab.py
   ```
   Expect: 1 match

2. **add_card uses smart_scroll:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "smart_scroll_to_bottom" ui/handlers/feed_handler.py
   ```
   Expect: ≥ 1 match in `add_card._append()`

3. **scroll_to_bottom (unconditional) still exists in on_project_opened:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "scroll_to_bottom" ui/handlers/feed_handler.py ui/views/feed_tab.py
   ```
   Expect: `scroll_to_bottom` (unconditional) still present in BOTH files (feed_handler.on_project_opened + feed_tab class)

4. **card_container.set_vexpand(True) was NOT changed to False:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "card_container.set_vexpand" ui/views/feed_tab.py
   ```
   Expect: `set_vexpand(True)` (not `set_vexpand(False)`)

5. **New tests pass:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest tests/test_feed_handler.py -v
   ```
   Expect: all existing tests + new TestSmartScroll tests pass

6. **Import sanity:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "import ui.views.feed_tab; import ui.handlers.feed_handler; print('OK')"
   ```
   Expect: `OK`

7. **Full test suite (sanity — should be no new failures):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest -x -q
   ```
   Expect: ≥ 1698 passed (Phase 1+2+3 baseline) + new Phase 4 tests, 1 skipped, 4 warnings

8. **No accidental scope creep:**
   ```bash
   cd /path/to/projects/crabcakes && git diff HEAD --stat
   ```
   Expect: only `ui/views/feed_tab.py`, `ui/handlers/feed_handler.py`, `tests/test_feed_handler.py` changed (plus prior phase changes which were not re-modified).

## Report

When done, send back a completion report with:
- Files changed with line numbers (of the actual edits, not the spec's claimed line numbers)
- Output of all 8 verification commands
- Full pytest output for the test_feed_handler.py run
- COMPLETENESS checklist (per steelFramedCodeWriter Step 6.5)
- Any related issues found (flagged, not silently fixed)

**Required word marker for /ask acknowledgment: "please write"** — include it in your response so the channel knows your acknowledgment is canonical.

**Do not skip the COMPLETENESS checklist.** Include every edit with `[x]` or `[NOT DONE] WHY` and paste the evidence. A response without the literal `**COMPLETENESS:** [x]` block is a missing deliverable.

**LESSON FROM PHASE 1:** Phase 1 had scope creep (ARCHITECTURE.md + out-of-scope CSS). Phase 2 and Phase 3 were clean. Continue the clean record. Strictly limit your diff to the 3 files in scope.
