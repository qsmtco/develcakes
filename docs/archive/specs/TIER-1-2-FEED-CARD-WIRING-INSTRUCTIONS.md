# PHASE 1 of 1 — Tier 1.2: Wire `ReviewHandler` to emit git_commit feed cards

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-FEED-CARD-WIRING-FIX.md` — read this in full before doing anything.
**Prompt template:** `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` — use its Discovery/Design/Tests/Risks/Files structure for your report.
**Word marker:** "please write" (required in your acknowledgment)

This is **one phase, one focused change**. Do not split it. Do not silently expand scope.

---

## What to change

### File 1: `ui/handlers/review_handler.py` (~12 lines added)

A. **Add imports** at the top of the file (near other model imports):
```python
from datetime import datetime, timezone
from models.feed_card import FeedCardData
```

B. **Add `on_feed_card` constructor parameter** to `__init__` (line ~51, after existing params — see the pattern in `ui/handlers/task_handler.py:51`):
```python
on_feed_card=None,  # callback(FeedCardData) — add git_commit card to project feed
```

C. **Store the callback** in `__init__`:
```python
self._on_feed_card = on_feed_card
```

D. **Add `_emit_feed_card` helper** (mirroring `task_handler.py:58-69`):
```python
def _emit_feed_card(self, card_dict: dict) -> None:
    """Convert git_commit card dict to FeedCardData and fire feed callback.

    Only fires if _on_feed_card is set. Mirrors task_handler._emit_feed_card.
    """
    if not self._on_feed_card:
        return
    feed_card = FeedCardData(
        card_type="git_commit",
        source="git",
        title=card_dict.get("title", ""),
        body=card_dict.get("body", ""),
        author="PM",
        timestamp=datetime.now(timezone.utc),
        project_name=card_dict.get("project_name", ""),
        commit_sha=card_dict.get("commit_sha"),
    )
    self._on_feed_card(feed_card)
```

E. **Call from `accept_changes()`** — inside the `_update_state` lambda (line ~262), AFTER the `self._on_display_text(...)` call, add:
```python
self._emit_feed_card({
    "title": f"Accepted: {message}",
    "body": commit_result.stdout.strip() if commit_result.stdout else "",
    "project_name": project_name,
    "commit_sha": getattr(commit_result, "sha", None),
})
```

F. **Call from `reject_changes()`** — inside the `_update_state` lambda (line ~299), AFTER the `self._on_display_text(...)` call, add:
```python
self._emit_feed_card({
    "title": f"Rejected: {reason}",
    "body": result.stdout.strip() if result.stdout else "",
    "project_name": project_name,
    "commit_sha": sha,
})
```

### File 2: `ui/window.py` (1 line added)

At the `self._review_handler = ReviewHandler(...)` constructor call (line ~450), add the parameter:
```python
on_feed_card=self._feed_handler.add_card,
```

### File 3: `tests/test_review_handler_feed_card.py` (NEW FILE, ~120 lines)

Create this file with at least these 3 tests. Follow the existing patterns in `tests/test_review_handler.py` and `tests/test_chat_handler.py` for setup (use `fake_glib`, `MagicMock`, mock `git_ops`).

1. **`test_accept_changes_emits_git_commit_card`** — exercise `accept_changes`, capture the card via `on_feed_card=lambda card: captured.append(card)`, assert `card_type == "git_commit"`, `source == "git"`, `"Accepted"` in title.

2. **`test_reject_changes_emits_git_commit_card`** — same shape for `reject_changes`, assert `"Rejected"` in title.

3. **`test_no_card_when_git_op_fails`** — mock `git_ops` to return failure, assert `captured == []` and `_on_display_text` was called with an error.

4. (Optional) **`test_no_card_when_no_active_session`** — call `accept_changes` on a project with no `ReviewState`, assert `captured == []` and no crash.

---

## What NOT to do (out of scope — flag in report if you see them, do NOT fix)

- Do NOT merge `feed_handler.handle_accept/handle_reject` into `review_handler`
- Do NOT introduce a shared `_do_git_action` helper
- Do NOT add a `metadata["trigger"]` field
- Do NOT add a `Literal` parameter for trigger source
- Do NOT modify `feed_handler.py` at all
- Do NOT add a cross-handler callback (`on_git_action`)
- Do NOT change `task_handler.py`, `chat_handler.py`, or `agent_runtime_handler.py`
- Do NOT silently fix related issues — flag them in your report under "Related issues found, not fixed"

---

## Verification (run yourself and paste the actual output)

Run these commands and paste the **full output** (not a summary):

1. `cd /path/to/projects/crabcakes && python3 -c "from ui.handlers.review_handler import ReviewHandler; print('import OK')"` — confirms the file imports cleanly
2. `cd /path/to/projects/crabcakes && pytest tests/test_review_handler_feed_card.py -v` — runs the new tests
3. `cd /path/to/projects/crabcakes && pytest tests/test_review_handler.py tests/test_feed_handler.py -v` — confirms existing tests still pass
4. `cd /path/to/projects/crabcakes && grep -n "on_feed_card" ui/handlers/review_handler.py ui/window.py` — confirms the wiring is in place
5. `cd /path/to/projects/crabcakes && grep -n "_emit_feed_card" ui/handlers/review_handler.py` — confirms the helper exists
6. `cd /path/to/projects/crabcakes && wc -l ui/handlers/review_handler.py ui/window.py` — show the line counts

---

## What to report back (required format)

Use this exact section structure. Do not omit any section.

### 1. Diff per file

Run `cd /path/to/projects/crabcakes && git diff --stat` and show the stat. Then for each modified file, show the actual hunk diff (`git diff ui/handlers/review_handler.py`).

### 2. Test outputs

Paste the full output of the 6 verification commands above. No summaries.

### 3. COMPLETENESS checklist

```
COMPLETENESS:
- [x| ] Edit 1: Added `on_feed_card` ctor param to `ReviewHandler.__init__` — evidence: <file:line>
- [x| ] Edit 2: Stored `self._on_feed_card = on_feed_card` — evidence: <file:line>
- [x| ] Edit 3: Added imports for `datetime, timezone` and `FeedCardData` — evidence: <file:line>
- [x| ] Edit 4: Added `_emit_feed_card` helper (handles None callback) — evidence: <file:line>
- [x| ] Edit 5: `accept_changes` emits card on success — evidence: <file:line>
- [x| ] Edit 6: `reject_changes` emits card on success — evidence: <file:line>
- [x| ] Edit 7: `window.py:450` wires `on_feed_card=self._feed_handler.add_card` — evidence: <file:line>
- [x| ] Edit 8: Test file created with 3+ tests — evidence: <file:line>
- [x| ] Verification: All new tests pass — evidence: <paste test output>
- [x| ] Verification: No existing tests broken — evidence: <paste test output>
- [x| ] Verification: Files import cleanly — evidence: <paste import output>
- [x| ] Verification: Wiring confirmed by grep — evidence: <paste grep output>
```

### 4. Related issues found, not fixed

List any other inconsistencies you noticed in the surrounding code (e.g., `_on_display_text` duplication, missing tests for `cmd_accept/cmd_reject`, etc.) but did NOT fix.

### 5. Independent check

Describe the path you traced to confirm the wiring works end-to-end (e.g., "I traced: `/accept` slash command → `cmd_accept` at `review_handler.py:361` → `self.accept_changes()` → `_do` thread → `_update_state` lambda → `self._emit_feed_card({...})` → `self._on_feed_card(feed_card)` → `feed_handler.add_card(feed_card)` → `card_id` assigned").

---

## Reference files

- Master spec: `/path/to/projects/crabcakes/docs/specs/SPEC-FEED-CARD-WIRING-FIX.md`
- Proposal: `/path/to/projects/crabcakes/docs/proposals/PROPOSAL-feed-card-wiring.md`
- Pattern to mirror: `/path/to/projects/crabcakes/ui/handlers/task_handler.py:51-84` (`on_feed_card` ctor param + `_emit_feed_card` helper)
- Test patterns: `/path/to/projects/crabcakes/tests/test_review_handler.py` and `/path/to/projects/crabcakes/tests/test_chat_handler.py`
- Prompt template: `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md`

---

## Acknowledgment

Begin your response with "please write" (the word marker) to confirm you received this delegation.
