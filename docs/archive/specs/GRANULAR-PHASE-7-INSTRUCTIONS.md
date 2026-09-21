# GRANULAR Phase 7 of 8 — Show Mode Auto-Approve + Card Widget Updates

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §3 (Exec auto-accept flow — Show mode)
**Files to change:** `ui/handlers/feed_handler.py` only

**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `ui/handlers/feed_handler.py` — focus on `add_card()` (line ~580), `_is_card_auto_acceptable()` (line ~372), `handle_accept()` (line ~1188), `handle_approve_exec()` (line ~1575)
2. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §3 (Exec auto-accept flow — Show mode, around line 1106)
3. `prompts/steelFramedCodeWriter.md` — your standing orders

## The Problem

Phase 4 wired `_is_card_auto_acceptable()` to correctly detect exec cards in Show mode (returns True when `exec_command.mode == "show"`). But `add_card()` line 622 unconditionally calls `handle_accept(cid)` for ALL auto-acceptable cards. For file-change cards this is correct (git stage+commit). For exec approval cards, `handle_accept` only marks `card.accepted = True` but **never calls `rt.approve_exec()`** — the agent's command is never approved.

## Task

Two changes, both in `ui/handlers/feed_handler.py`:

### Change 1: Route exec cards through `handle_approve_exec` in `add_card()`

Find the auto-accept block in `add_card()` (inside the `_append()` closure, around line 618-623):

```python
if card_data.accepted is None and self._is_card_auto_acceptable(card_data):
    self._GLib.idle_add(lambda cid=card_data.card_id: self.handle_accept(cid))
```

Replace with:

```python
if card_data.accepted is None and self._is_card_auto_acceptable(card_data):
    # Exec approval cards in Show mode: auto-approve (not git accept)
    # and hide the Approve/Deny buttons so the user can't double-act.
    # Silent mode never reaches here (bypassed in AgentRuntimeHandler).
    if (card_data.card_type == "agent_action"
            and card_data.metadata.get("needs_approval")):
        self._GLib.idle_add(
            lambda cid=card_data.card_id: self._auto_approve_exec_card(cid)
        )
    else:
        self._GLib.idle_add(lambda cid=card_data.card_id: self.handle_accept(cid))
```

### Change 2: Add `_auto_approve_exec_card()` method

Add this method near `handle_approve_exec()` (around line 1575):

```python
def _auto_approve_exec_card(self, card_id: str) -> None:
    """Auto-approve an exec card in Show mode.

    Called from add_card() via GLib.idle_add when _is_card_auto_acceptable
    returns True for an exec approval card. Does three things:
    1. Calls handle_approve_exec(card_id, True) to approve the command
       via AgentRuntimeHandler.approve_exec().
    2. Hides the Approve/Deny buttons on the card widget via
       feed_tab.hide_card_buttons() so the user can't double-act.
    3. Updates the card visual to show "approved" state.

    Silent mode never reaches here — AgentRuntimeHandler._do_approval_needed
    bypasses card creation entirely when mode == "silent".

    Args:
        card_id: The card to auto-approve.
    """
    # 1. Approve the command via the registered callback
    self.handle_approve_exec(card_id, True)

    # 2. Hide Approve/Deny buttons on the card widget
    if self._feed_tab is not None:
        try:
            self._feed_tab.hide_card_buttons(card_id, ["approve", "deny"])
        except AttributeError:
            # MockFeedTab or legacy FeedTab without hide_card_buttons
            pass

    # 3. Update visual to show approved state
    card = self._cards.get(card_id)
    if card is not None:
        card.accepted = True
        self._update_card_visual(card_id, accepted=True)
```

### DO NOT:
- Modify `_is_card_auto_acceptable()` (already correct from Phase 4)
- Modify `handle_approve_exec()` (already correct)
- Modify `handle_accept()` (file-change path is correct)
- Modify any other files
- Add tests (Phase 8)

## Verification

```bash
# Verify file parses
python3 -c "import ast; ast.parse(open('ui/handlers/feed_handler.py').read()); print('AST OK')"

# Verify new method exists
grep -n "def _auto_approve_exec_card" ui/handlers/feed_handler.py

# Verify add_card routing logic
grep -n "_auto_approve_exec_card\|agent_action.*needs_approval" ui/handlers/feed_handler.py

# Run ALL tests
python3 -m pytest tests/test_feed_handler.py tests/test_feed_card.py tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_crabcard_parser.py tests/test_crabwatch_handler.py -q

# Line count
wc -l ui/handlers/feed_handler.py
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] Change 1: add_card routes exec cards through _auto_approve_exec_card — evidence (grep)
- [x/not done] Change 2: _auto_approve_exec_card method added — evidence (grep)
- [x/not done] _auto_approve_exec_card calls handle_approve_exec — evidence (grep/sed)
- [x/not done] _auto_approve_exec_card calls hide_card_buttons — evidence (grep/sed)
- [x/not done] _auto_approve_exec_card updates card visual — evidence (grep/sed)
- [x/not done] _is_card_auto_acceptable NOT modified — evidence (diff or grep)
- [x/not done] handle_approve_exec NOT modified — evidence (diff or grep)
- [x/not done] All existing tests pass — evidence (pytest output)
```
