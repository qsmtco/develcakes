# SPEC-15c: Telegram Phone Shows Supervisor Replies Only

**Date:** 2026-10-16
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** PM direction 2026-10-16 — the connected Telegram bridge must stop mirroring feed approval cards. The phone shows Supervisor turn replies.
**Depends on:** SPEC-15 (Telegram Remote Bridge — DONE). Amends the outbound half of SP3a. Does not reopen SP2, SP3b, SP4, or SPEC-15b.
**Target branch:** main

> Architecture compliance: the bridge stays a handler node under `ui/handlers/`. `ui/window.py` remains the composition root and still does not let the bridge import window or `AgentRuntimeHandler`. No new module, no new transport method, no change to the feed card model. `docs/ARCHITECTURE.md` §3.2a is the owner of this contract and must be updated in the same change (section 8).

---

## Discovery (verified against source)

- Read `ui/window.py` `_wire_feed` (FeedHandler constructed with no `on_card_added`) and lines 515–518: the only production registration of `FeedHandler.set_card_added_callback` passes `self._bridge_handler.on_feed_card_added`. `_on_connect_clicked` (1112–1140) only calls `start_bridge` / `stop_bridge`. `_on_agent_response` (1152–1167) calls `bridge.on_supervisor_reply(session_key, text)` and swallows any exception from that call.
- Read `ui/handlers/telegram_bridge_handler.py`: `SUPERVISOR_KEY = "special:supervisor"`. `on_feed_card_added(self, card_id_or_data: Any) -> None` (692–739) sends `⚠️ Approval requested — {command}` with an inline keyboard when `metadata["needs_approval"] is True` and `metadata["status"] == "pending_approval"` and `_transport` and `_chat_id` are set. `on_supervisor_reply(self, session_key: str, text: str) -> None` (776–785) returns immediately unless `session_key == SUPERVISOR_KEY`, then `forward_to_phone`. `send_message` from this method is the spam. `_handle_callback_query` (620–690) is inbound: a tap already on the phone calls `approve_exec` and `edit_message_text`. `set_feed_handler` (210–217) exists so that re-read can happen.
- Read `ui/handlers/feed_handler.py` `set_card_added_callback(self, callback) -> None` (193–197). `add_card` calls `self._on_card_added(card_id)` with the **string** card id (448–449), inside the append idle, while the card is still `pending_approval` — before the Show-mode auto-approve idle. `add_cards_batch` does the same per id (573–575). A falsy callback skips both sites.
- Read `ui/handlers/agent_runtime_handler.py` `_do_approval_needed` (1413–1487) and `request_live_bridge_approval` (1364–1410): both `add_card` a card with `needs_approval: True` and `status: "pending_approval"`. Silent exec auto-accept returns before `add_card`. Show mode still adds the card. Desk Approve/Deny stays on that card.
- Read `ui/agent_runtime/turn_ui.py` `_do_response_complete` (611–614): fires `_on_agent_response(session_key, text, project_name)` with the full turn text. Tool-running cards (`status: "running"`) are a different `add_card` and already fail the approval allowlist.
- Read `transport/telegram.py` `send_message(self, text: str, reply_markup: dict | None = None, chat_id: int | None = None) -> dict` (260–276). Raises `ValueError` when no chat id is available. Not modified.
- Read `tests/test_telegram_bridge_handler.py`: `_connected()` / `FakeTransport.sent`. No test calls `on_feed_card_added`. Inbound tests `test_callback_query_approve_resolves_and_edits` and `test_callback_query_unknown_state_says_already_resolved` must keep passing. SP3b tests `test_supervisor_reply_forwards_to_phone` and `test_non_supervisor_reply_is_not_forwarded` must keep passing.
- Read `tests/test_window_telegram_bridge.py` `TestAgentResponseComposition` and `TestDocsPresence`. No assertion requires `set_card_added_callback`. `test_readme_has_remote_in_bullet` requires the strings `Remote-In from Your Phone` and `thin client` to remain in `README.md`. `test_architecture_documents_telegram` requires `transport/telegram.py` and `TelegramBridgeHandler` to remain in `docs/ARCHITECTURE.md`.
- Architecture owner: `docs/ARCHITECTURE.md` §3.2a. The exec-approval outbound bullet (lines 360–362) is the sentence this spec replaces.
- Existing pattern to copy: SP3b already drops non-Supervisor replies inside `on_supervisor_reply`. This spec stops the other outbound feed path. It does not add a second filter.

---

## 1. Overview

### Problem

After Connect, every new exec-approval feed card is sent to the paired Telegram chat as `⚠️ Approval requested — $ {command}` with Approve and Deny buttons. `_do_approval_needed` creates that card for every `exec_command` (and `request_live_bridge_approval` creates the same shape). Show-mode auto-accept still creates the card; the phone send happens in the same append callback, before auto-approve. A busy agent therefore spams the phone with command-approval cards. The in-app feed is the right place for those cards. The phone is not.

### Solution

Stop the outbound approval mirror. Leave Supervisor turn replies on the path that already exists: turn complete → `_on_agent_response` → `on_supervisor_reply` → `forward_to_phone`. Approval cards continue to be created and resolved in the app.

### Scope

| In | Out |
|---|---|
| `on_feed_card_added` becomes a no-op that never calls `send_message` | Changing how approval cards are created, auto-approved, or clicked in the app |
| Remove the window registration of that callback | Changing `on_supervisor_reply`, `forward_to_phone`, HTML flatten, or the 4096 split |
| Docstring / ARCHITECTURE / README so they stop promising phone approval buttons | Inbound `/status`, `/stop`, `/help`, plain text to the Supervisor, foreign-chat refusal |
| One regression test that a pending-approval card sends nothing | Deleting `_handle_callback_query`, `_approval_msgs`, `set_feed_handler`, or `approve_exec` |
| | `transport/telegram.py`, `agent/runtime.py`, tool-running feed cards |

### Principles

- Handler isolation stays: the bridge still does not import window or the feed handler module.
- Single outbound conversation path: Supervisor final text only, already keyed on `SUPERVISOR_KEY`.
- Desk approvals are unchanged. The phone stops being a second approval surface for new cards.
- A tap on an approval message already sitting in the chat still resolves. That is inbound, not a new send.

---

## 2. Changes by File

### 2.1 `ui/handlers/telegram_bridge_handler.py`

Replace the body of `on_feed_card_added`. Keep the name and signature so a stale caller cannot raise `AttributeError` into `FeedHandler._append`. The method must not look up the card, must not touch `_approval_msgs`, and must not call `send_message`.

Current signature (do not change):

```python
def on_feed_card_added(self, card_id_or_data: Any) -> None:
```

Replacement (the argument is intentionally unused):

```python
def on_feed_card_added(self, card_id_or_data: Any) -> None:
    """SPEC-15c: feed cards are not sent to the phone.

    Supervisor turn text goes out through ``on_supervisor_reply``.
    Approval cards stay in the app feed. A tap on an approval message
    that was sent before this change still resolves in
    ``_handle_callback_query``.
    """
    return None
```

`return None` matches the annotated return. `card_id_or_data` may be a `str` card id (what `add_card` passes), a card object, or anything else. The method does not inspect it, so it cannot raise `AttributeError` or the `get_card` exception the old body logged.

Update the module comment at the top of the file:

- Line 12–13 today says scope includes "exec approvals via inline callback buttons (SP3a)". Change that clause to: inbound taps on approval messages already in the chat still resolve (SP3a inbound); new feed cards are not mirrored (SPEC-15c).
- Lines 17–18 today say the phone gets "conversation + approvals". Change to: the phone gets Supervisor conversation plus the `/status` `/stop` `/help` allowlist. It does not receive new approval cards.

Do not edit `on_supervisor_reply`, `forward_to_phone`, `_send`, `_handle_callback_query`, `set_feed_handler`, `start_bridge`, or `stop_bridge`.

Estimate: about −45 / +15 lines.

### 2.2 `ui/window.py`

Delete the registration in `_wire_feed`. Today:

```python
        # SPEC-15 SP3b: pending approval cards mirror to the phone (inline
        # buttons). The bridge filters to its own allowlist internally.
        self._feed_handler.set_card_added_callback(
            self._bridge_handler.on_feed_card_added)
```

Delete those four lines. `FeedHandler.__init__` is called above without `on_card_added`, so `_on_card_added` stays `None`. Both `add_card` and `add_cards_batch` already skip a falsy callback.

Leave these wires in place:

- `set_feed_handler(self._feed_handler)` — still required so `_handle_callback_query` can re-read a card.
- `set_on_agent_response(self._on_agent_response)` and the body of `_on_agent_response`. That is the Supervisor reply path. It passes `(session_key, text)` into `on_supervisor_reply`. `project_name` is not forwarded. Do not change that call.

Estimate: −4 lines.

### 2.3 `ui/handlers/feed_handler.py`

Update only the docstring of `set_card_added_callback`. The method signature stays:

```python
def set_card_added_callback(self, callback) -> None:
```

The current docstring says the Telegram bridge filters pending approvals from this seam. Replace that sentence. The setter still assigns `self._on_card_added = callback`, and `None` still clears it. Production no longer registers a callback. The seam remains for tests and any future observer.

Do not change `add_card` or `add_cards_batch`.

Estimate: docstring only, ~4 lines.

### 2.4 `docs/ARCHITECTURE.md` §3.2a

Replace the exec-approvals bullet (the one that starts "Exec approvals: pending-approval cards mirror to the phone") with:

- Exec approvals stay in the app feed. New approval cards are not sent to the phone (SPEC-15c). A tap on an approval message already in the chat still calls `ARH.approve_exec`, re-reads the card, and edits that message to the actual state.

Leave the outbound-mirror bullet (Supervisor turn replies, 4096 split, html flatten) as it is. Leave the handler-isolation paragraph. Keep the strings `transport/telegram.py` and `TelegramBridgeHandler` (tests assert both).

### 2.5 `README.md`

In the "Remote-In from Your Phone" section, replace the clause that promises exec-approval prompts as inline buttons. The heading `Remote-In from Your Phone` and the words `thin client` must remain (`tests/test_window_telegram_bridge.py` `test_readme_has_remote_in_bullet`).

Replacement sentence, same paragraph:

Connect the **Supervisor to your phone over Telegram** — click Connect in the toolbar and pair a bot in Settings. Your phone becomes a thin client: chat back and forth with the Supervisor, and run a small remote allowlist (`/status`, `/stop` with a typed confirmation, `/help`). Command approvals stay in the app feed. No shell, no file access.

### 2.6 `tests/test_telegram_bridge_handler.py`

Add this test next to the existing SP3b tests. It uses `_connected()` already in the file. `FakeTransport.send_message` appends to `self.sent` and returns `{"ok": True}` (no `result.message_id`). Asserting `sent == []` is the proof that nothing was sent.

```python
def test_feed_card_is_not_sent_to_phone():
    """SPEC-15c: a pending exec-approval card must not reach the phone."""
    h, t, _a = _connected()
    card = type("C", (), {
        "card_id": "card-1",
        "body": "$ ls",
        "metadata": {
            "needs_approval": True,
            "status": "pending_approval",
        },
    })()
    h.on_feed_card_added(card)
    h.on_feed_card_added("card-1")  # add_card passes the id string
    h.on_feed_card_added(None)
    assert t["t"].sent == []
    assert h._approval_msgs == {}
```

`_connected()` calls `start_bridge` then the captured `on_connect`, which sets state to connected and leaves `_transport` and `_chat_id` set. That is the condition under which the old method sent. The test is red before the no-op and green after.

Do not delete `test_callback_query_approve_resolves_and_edits` or `test_callback_query_unknown_state_says_already_resolved`.

### 2.7 `tests/test_window_telegram_bridge.py`

Add a source-shape assertion beside `test_build_registers_composed_callback`:

```python
def test_build_does_not_mirror_feed_cards_to_phone(self):
    import pathlib

    src = pathlib.Path("ui/window.py").read_text()
    assert "on_feed_card_added" not in src
    assert "set_card_added_callback" not in src
```

`test_build_registers_composed_callback` must still find `set_on_agent_response(self._on_agent_response)`.

### Files NOT changed

- `agent/runtime.py` — `_dispatch_approval` still asks the app for approval.
- `ui/handlers/agent_runtime_handler.py` — `_do_approval_needed` and `request_live_bridge_approval` still create in-app cards. `approve_exec` still resolves them.
- `ui/agent_runtime/turn_ui.py` — `_do_response_complete` still calls `_on_agent_response` with the full turn text.
- `transport/telegram.py` — `send_message` / `edit_message_text` / `answer_callback_query` stay. Supervisor replies and allowlist replies still use them.
- `ui/handlers/telegram_bridge_handler.py` methods other than `on_feed_card_added` and the module comment.
- `utils/feed_store.py`, `models/feed_card.py`, `ui/views/feed_card.py` — card shape and Approve/Deny buttons in the app stay.

---

## 3. Data Flow

### After Connect — what the phone receives

1. Toolbar Connect → `Window._on_connect_clicked` → `TelegramBridgeHandler.start_bridge`. No feed replay. `_chat_id` is set from the store. `_transport` is the live `TelegramTransport`.
2. An agent finishes a turn → `turn_ui._do_response_complete` → `Window._on_agent_response(session_key, text, project_name)`.
3. That calls `on_supervisor_reply(session_key, text)`.
4. If `session_key != "special:supervisor"`, return. Nothing is sent. Coder, Debugger, and every other agent stay off the phone.
5. If it matches, `forward_to_phone` sends the flattened text in chunks of at most 4096 characters. Return value of each `send_message` is ignored; a chunk exception is logged and the next chunk is attempted. That behavior is unchanged.

### After Connect — what no longer reaches the phone

1. `exec_command` (or a sensitive write/edit, or a live-page approval) → `AgentRuntimeHandler._do_approval_needed` or `request_live_bridge_approval`.
2. `FeedHandler.add_card` stores the card and, on the main thread, calls `_on_card_added(card_id)` only when a callback is set.
3. After this spec, no callback is set. The card is appended to the feed tab with Approve/Deny. The phone gets no message and `_approval_msgs` is not updated.
4. The PM approves or denies on the desk card. `approve_exec` resolves the runtime wait. Telegram is not involved.

### Phone commands (unchanged)

Paired-chat text that is not `/status`, `/stop`, or `/help` still goes to `send_to_special_agent("special:supervisor", text)`. Those three commands still answer on the phone. A foreign chat still gets one refusal via `send_message(..., chat_id=foreign_id)`.

### Leftover approval buttons (unchanged inbound)

`_handle_callback_query` still accepts `approve:<card_id>` / `deny:<card_id>` from the paired chat, calls `approve_exec(card_id, approved)` (return value is `None`; ignore it), re-reads the card through `set_feed_handler`'s `get_card`, and `edit_message_text`s the message id stored in `_approval_msgs`. New cards never enter that map, so this path only serves messages sent before the fix.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `ui/handlers/telegram_bridge_handler.py` | `on_feed_card_added` no-op; module comment | Low. Stops the send. Inbound tap path untouched. |
| `ui/window.py` | Remove `set_card_added_callback` registration | Low. Feed callback becomes unused. Reply composition stays. |
| `ui/handlers/feed_handler.py` | Docstring of `set_card_added_callback` only | Low. |
| `docs/ARCHITECTURE.md` | §3.2a exec-approval bullet | Low. Test strings preserved. |
| `README.md` | Remote-In paragraph | Low. Heading and "thin client" preserved. |
| `tests/test_telegram_bridge_handler.py` | New no-send test | Low. |
| `tests/test_window_telegram_bridge.py` | Source-shape guard | Low. |

---

## 5. Implementation Order

1. Add `test_feed_card_is_not_sent_to_phone` and run it. It must fail while `on_feed_card_added` still calls `send_message`.
2. Replace `on_feed_card_added` with the no-op in §2.1. Re-run that test. It must pass. Re-run `test_supervisor_reply_forwards_to_phone`, `test_non_supervisor_reply_is_not_forwarded`, and `test_callback_query_approve_resolves_and_edits`. They must still pass.
3. Delete the four-line registration in `ui/window.py`. Add `test_build_does_not_mirror_feed_cards_to_phone`. Both that test and `test_build_registers_composed_callback` must pass.
4. Update the `set_card_added_callback` docstring, `docs/ARCHITECTURE.md` §3.2a, and the README Remote-In paragraph.
5. Run:

```bash
python -m pytest tests/test_telegram_bridge_handler.py tests/test_window_telegram_bridge.py -q
```

6. Confirm the old send is gone from the handler:

```bash
grep -n "Approval requested" ui/handlers/telegram_bridge_handler.py
```

That command must print nothing. `grep -n "on_feed_card_added" ui/window.py` must print nothing.

---

## 6. Acceptance Criteria

- [ ] With the bridge connected, calling `on_feed_card_added` with a pending-approval card, a card-id string, or `None` leaves `FakeTransport.sent` empty and `_approval_msgs` empty.
- [ ] `ui/window.py` does not call `set_card_added_callback` and does not name `on_feed_card_added`.
- [ ] A Supervisor turn reply (`session_key == "special:supervisor"`) is still forwarded by `on_supervisor_reply`.
- [ ] Any other session key is still not forwarded.
- [ ] An inbound `approve:<id>` callback from the paired chat still calls `approve_exec` and edits the stored message.
- [ ] `/status`, `/stop`, `/help`, plain Supervisor text, and the foreign-chat refusal behave as they do today.
- [ ] In-app approval cards are still created for `exec_command` when exec auto-accept is not silent, and desk Approve/Deny still resolves them.
- [ ] ARCHITECTURE §3.2a no longer says pending-approval cards mirror to the phone. README no longer promises inline approval buttons. Both files still satisfy the existing source-shape tests.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Bridge disconnected (`_transport` is `None`) | `on_feed_card_added` returns. `on_supervisor_reply` already no-ops. No raise. |
| Show-mode auto-accept | Card is still created and then auto-approved in the app. No Telegram message. |
| Silent exec auto-accept | No card, same as today. No Telegram message. |
| Tool-running card (`status: "running"`, no `needs_approval`) | Already not sent. Still not sent. |
| Live-page approval card | Still an in-app card. Not sent. |
| Non-Supervisor agent finishes a turn | `on_supervisor_reply` returns before `forward_to_phone`. |
| Supervisor reply is empty, `None`, or whitespace-only chunks | Existing `forward_to_phone` guards. Unchanged. |
| Approval message already in the Telegram chat, user taps Approve | `_handle_callback_query` runs. If `_approval_msgs` still has the id, the message is edited. If the map was cleared by disconnect, the existing "Already resolved in the app." answer path runs. |
| `on_feed_card_added` called with a non-card object | Returns `None`. Does not raise. |
| Two Connect clicks | Existing state machine. This spec does not change it. |
| Feed append while the bridge is connected | `add_card` does not call the bridge, because the callback is unset. |

---

## 8. ARCHITECTURE.md Updates Required

§3.2a, the exec-approvals bullet only, as specified in §2.4. No other architecture section describes the outbound approval mirror.
