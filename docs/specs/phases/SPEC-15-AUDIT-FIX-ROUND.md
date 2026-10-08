# SPEC-15 — Audit Fix Round (Debugger findings, 2026-10-07)

**Source:** Debugger's adversarial audit of commits `c7cbb1cd` + `9c1c179f`. Findings
reproduced by the Supervisor by code reading. Fix ALL items below.

Files in scope: `ui/handlers/telegram_bridge_handler.py`, `transport/telegram.py`
(only for F1b/F4), `ui/views/settings_dialog.py` (F8), tests.

Word marker: **please write**.

---

## MUST FIX (gate DONE)

### F1 — transport stacking on ERROR→reconnect  [bug, resource-leak]
`_on_transport_error` sets `state=ERROR` but never stops `self._transport`; the
transport's `_poll_loop` KEEPS looping after a soft error (`transport/telegram.py:355`
fires on_error then `continue`s). `start_bridge` (`telegram_bridge_handler.py:264`)
unconditionally constructs a NEW transport with no teardown of the old one ⇒ TWO live
poll loops ⇒ the Supervisor receives every phone message TWICE.

- **F1a:** at the TOP of `start_bridge`, tear down any existing transport (call
  `self.stop_bridge()` — it is idempotent — BEFORE the config checks, or right before
  constructing the new transport). Guarantee exactly ONE live transport per session.
- **F1b (honesty):** today `on_connect` fires only ONCE per transport (`announced`
  latch, `transport/telegram.py:331/360`), so after a recoverable soft error the
  handler is stuck in ERROR forever while the bridge actually works (toolbar lies
  "● Offline"). Fix in `_poll_loop`: when a soft error occurred, reset the announce
  latch so the NEXT successful `getUpdates` re-fires `on_connect`. The handler then
  recovers ERROR → CONNECTED on its own.

### F2 — armed /stop confirmation survives ERROR→reconnect  [bug, stale-state]
`_pending_stop` is cleared only in `stop_bridge` + `_on_transport_disconnect`. A bare
`STOP` after an error+reconnect fires stop-all on a "later session" — exactly the class
§SP4 B4 forbids. Clear `_pending_stop` in `_on_transport_error` (and it is already
handled by F1a's `stop_bridge()` at start_bridge, but clear explicitly in the error
path too).

### F3 — foreign-chat refusal is misdelivered to the PAIRED chat  [spec divergence]
`_send_refusal(chat_id)` (`:602`) IGNORES its argument and sends via `_send` →
`send_message`, which ALWAYS targets `self.chat_id` (the paired chat). So the PM sees a
confusing "This bot is paired to another chat" on their OWN phone, and the stranger
gets nothing. Spec §1/§4/§5: the FOREIGN chat gets one polite refusal.

- **RULING (Supervisor):** implement option (a) — deliver the refusal to the FOREIGN
  chat. Add an optional `chat_id: int | None = None` override to
  `TelegramTransport.send_message(text, reply_markup=None, chat_id=None)` (None → the
  paired chat, preserving every existing caller). `_send_refusal` passes the foreign id.
- Update the test FakeTransport to MODEL the target chat (`(chat_id, text)`), and assert
  the refusal targets the FOREIGN id and NOTHING is sent to the paired chat.

### F4 — partial §SP4 scope  [issue, partial-scope-completion]
Add the missing deliverables: **settings tooltips** on the Telegram Bridge section
(`ui/views/settings_dialog.py` — token field + Test button; that section has zero
tooltips today). (The context.md entry + post-mortem are the Supervisor's to write —
do NOT do those.)

---

## CHEAP SUGGESTIONS (fold in)

### F5 — `_telegram_text` HTML-flatten leakage  [suggestion, naive-tag-strip]
`<[^>]+>` is not attribute-aware and `html.unescape` runs AFTER the strip, so
`<div title="a > b">x</div>` → `b">x`, and `&lt;script&gt;` → `<script>`. Strip tags
BEFORE and AFTER `html.unescape` (double-strip) or use an attribute-aware pass. Add
test cases for `>`-in-attribute and entity-encoded tags.

### F6 — `forward_to_phone` sends an EMPTY first chunk  [suggestion, empty-chunk-send]
`' '*5000` → `['', ' '*904]` — the empty chunk is still sent (Telegram 400).
Skip empties: `if chunk: send(...)`. Add whitespace-only + all-newline tests asserting
ZERO sends. (Boundary math is otherwise correct — do not change it.)

### F7 — unguarded `int(chat_id)` in `start_bridge`  [suggestion, unguarded-cast]
`self._chat_id = int(chat_id)` (`:263`) can raise ValueError into the GTK click handler
for an odd injected config. Wrap → `_set_state(ERROR)`, never raise. Add a test.

---

## RED-first + verification

RED-first for F1 (start → on_error → start again ⇒ exactly ONE live transport; only one
delivery) and F2 (armed /stop → error → reconnect ⇒ a later bare STOP does NOT fire).

Final battery (paste REAL output):
```bash
.venv/bin/python -m pytest tests/test_telegram_bridge_handler.py tests/test_telegram_store.py tests/test_telegram_transport.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_window_telegram_bridge.py tests/test_settings_telegram_section.py tests/test_toolbar.py -q
.venv/bin/python -m ruff check ui/handlers/telegram_bridge_handler.py transport/telegram.py ui/views/settings_dialog.py tests/test_telegram_bridge_handler.py tests/test_telegram_transport.py
.venv/bin/python -m pyright ui/handlers/telegram_bridge_handler.py transport/telegram.py 2>&1 | tail -3
```

## NOT in this round (Supervisor-registered follow-ups — do NOT implement)
- Blocking Telegram sends on the GTK main thread (Debugger "issue") — needs a
  worker-thread/async design; registered as its own unit.
- `ui/window.py:751` chat_handler response slot un-composed — both paths are DEAD today
  (Debugger verified). Registered as a cleanup follow-up.
