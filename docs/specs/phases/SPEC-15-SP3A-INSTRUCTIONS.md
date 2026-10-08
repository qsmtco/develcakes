# SPEC-15 SP3a — Phase Instructions: Exec Approvals via Telegram (phone side)

**Spec:** `docs/specs/SPEC-15-TELEGRAM-REMOTE-BRIDGE.md` §SP3 (read it in full FIRST —
this file is the phase plan; the spec is the contract).
**Files in scope:** `ui/handlers/telegram_bridge_handler.py`, `transport/telegram.py`
(only if a send/edit shape needs a fix), `tests/test_telegram_bridge.py` (or the file
SP2 used — check `ls tests/ | grep -i telegram`).
**Out of scope:** the app→phone reply mirror (`forward_to_phone`) — that is SP3b.
No window.py changes in this phase. No ARH changes in this phase.

---

## 0. Baseline (before any edit)

```bash
ls tests/ | grep -i telegram
python -m pytest tests/test_telegram_bridge.py -q   # or the actual SP2 test file(s)
ruff check ui/handlers/telegram_bridge_handler.py transport/telegram.py
```

Record counts verbatim.

## 1. What exists (verified 2026-10-07)

- `TelegramBridgeHandler._handle_callback_query` (telegram_bridge_handler.py:291) is the
  SP2 stub: logs "not yet wired", answers the callback with
  "Approvals are not yet wired (SP3)." → THIS phase replaces it.
- `ARH.approve_exec(approval_id, approved)` (agent_runtime_handler.py:949) resolves a
  pending approval: pops `_pending_approvals[approval_id]`, forwards to the owning
  runtime, and rewrites the feed card metadata (`status` = approved/denied,
  `accepted` = bool). **It returns None in ALL cases** — an unknown/already-resolved id
  just logs a warning and returns. The bridge CANNOT learn the outcome from the return
  value; it must RE-READ the card (§3 step 4).
- `FeedCardData.metadata` on approval cards: `needs_approval=True`,
  `status="pending_approval"`, `session_key`, `tool_name`, `tool_args` (built in
  `_do_approval_needed`, agent_runtime_handler.py:~2345).
- `TelegramTransport.send_message(text, reply_markup=None) -> dict` (transport/telegram.py:254),
  `answer_callback_query(id, text)` (:267), `edit_message_text(message_id, text, ...)`
  (:274). `reply_markup` is the raw Telegram dict — inline keyboards ride it.
- Window wires `on_card_added` NOWHERE today (ui/window.py FeedHandler construction
  :436-445 omits it) — the seam is FREE, but the subscription wiring is SP3b. SP3a
  builds the bridge-side surface and tests it through the bridge's own API.

## 2. The change (bridge file only)

### 2.1 Approval bookkeeping

- Add a bounded map `card_id → message_id` (FIFO cap 50, same discipline as the
  review queues) recording the Telegram message that carries each approval's buttons.
- Add `on_feed_card_added(card_id_or_data)` — the SP3b hook will call it from window's
  `on_card_added`. In SP3a, implement + unit-test it directly: filter
  (`needs_approval is True` AND `status == "pending_approval"`), build the text
  (title + `$ command` body), `send_message` with
  `reply_markup={"inline_keyboard": [[
    {"text": "✅ Approve", "callback_data": f"approve:{card_id}"},
    {"text": "❌ Deny", "callback_data": f"deny:{card_id}"}]]}`,
  store the returned message_id. **Only `callback_data` from this exact vocabulary is
  ever trusted — see 2.2.** Card ids are platform UUIDs — never embed any
  page/chat-derived string in callback_data.
- Skip + log when disconnected, unpaired, or the card lacks approval metadata.
  `needs_review` cards and every non-approval card type NEVER surface (allowlist).

### 2.2 `_handle_callback_query` — the real wiring

Replace the stub. Order of guards (each failure → answerCallbackQuery no-op notice,
never a crash):

1. Foreign chat → current behavior stays: return WITHOUT answering (do not teach a
   foreign chat that the bot lives here).
2. Parse `data`: must be exactly `approve:<card_id>` or `deny:<card_id>` (split on the
   FIRST colon; anything else → answer "Unrecognized action" no-op).
3. `answer_callback_query(cbq_id, "…")` with a short ack text.
4. Resolve: call `self._arh.approve_exec(card_id, approved)` (bridge already holds the
   ARH ref; the call is main-thread — the dispatch seam guarantees it). Then RE-READ
   the card via a feed-handler reference (see 2.3) and edit the Telegram message to
   the ACTUAL outcome:
   - card `status == "approved"/"denied"` → edit to `✅ Approved — $ cmd` / `❌ Denied — $ cmd`
     (use the command preview from the callback payload context or the card body).
   - card missing or still pending (desk resolved first and removed it, or a duplicate
     second tap) → edit to "Already resolved in the app." — NO second approve_exec call
     effect can occur (approve_exec pops-first; a resolved id is a logged no-op), but
     the edit must say the truth, not the tap's intent.
5. Never re-send a fresh approval message from a callback.

### 2.3 Feed-handler access

Setter-inject a feed handler reference on the bridge (`set_feed_handler(...)`, house
pattern — the bridge must NOT import FeedHandler's module). SP3a uses it for the
post-approve card re-read. SP3b's window wiring passes the real handler. In SP3a tests,
inject a stub exposing `get_card(card_id)`.

**Guard:** if no feed handler is set (or get_card returns None), the callback still
answers + edits ("state unknown — check the app"), and NO exception escapes the bridge
(existing BLE001 discipline).

### 2.4 Disconnect hygiene

On `stop_bridge` (and transport disconnect), clear the card→message map. A stale map
must never drive `edit_message_text` on a dead session.

## 3. Tests (RED-first per steelFramedCodeWriter — failing assert BEFORE the fix)

Fake transport battery (reuse the SP2 fake pattern):

| Case | Assert |
|---|---|
| Approval card added → message sent | sendMessage called once with inline_keyboard containing `approve:<id>`/`deny:<id>` and the command preview in the text |
| Non-approval cards never surface | `needs_review` card + plain message card → no sendMessage |
| Tap Approve | `arh.approve_exec(card_id, True)` called exactly once; message edited to approved text; callback answered |
| Tap Deny | same with False + denied text |
| Desk-first race | card already resolved (stub returns status="approved") → approve_exec STILL called? NO — assert the edit says "Already resolved in the app." and approve_exec is NOT called when the pre-read shows resolved; if your implementation calls-then-reads, the post-read must govern the edit — pick ONE ordering and pin it. |
| Stale/duplicate tap | second tap → one no-op answer + "Already resolved" edit; approve_exec NOT re-invoked on an id whose card is resolved |
| Foreign chat callback | no answerCallbackQuery, no approve_exec |
| Malformed callback_data | answered "Unrecognized action"; approve_exec not called |
| No feed handler / card missing | no crash; edit says state unknown |
| Disconnect clears map | after stop_bridge, a callback for an old card_id → "Already resolved in the app." edit never references a dead message |

Test fidelity: drive the PUBLIC bridge methods (`on_feed_card_added`, the
`_handle_update`-entry callback path), not private helpers — same rule as SPEC-17 SP1.

## 4. Verification (paste ALL outputs)

```bash
python -m pytest tests/test_telegram_bridge.py -q        # + the SP2 test file if separate
ruff check ui/handlers/telegram_bridge_handler.py transport/telegram.py
pyright ui/handlers/telegram_bridge_handler.py 2>&1 | tail -3
```

NO NEW ruff findings vs baseline. Redaction: any log line printing callback_data or
command text keeps the `redact_log_preview` pattern.

## 5. Report format (mandatory)

Files changed + line numbers, baseline vs after counts, all command outputs verbatim,
COMPLETENESS checklist (one line per §2 item + each test row), related-bug scan
(flagged, not fixed).

Word marker for this delegation: **please write**.
