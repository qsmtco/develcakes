# SPEC-15 Post-Mortem — Telegram Remote Bridge

**Date:** 2026-10-07
**Status:** DONE — SP1 + SP2 + SP3a + SP3b + SP4 landed; adversarial audit CLEAN.
**Commits:** `4296bd3b` (SP1), `58a33d77` (SP2), `c7cbb1cd` (SP3a + SP3b body),
`9c1c179f` (SP3b trigger + SP4), `7915c669` (audit round 1),
`27a28cc4` (audit round 2), `bb74191c` (on_update guard).

## What shipped

The PM's "Connect = bridge the Supervisor to your phone" direction, implemented end to
end:

- **`transport/telegram.py`** — `TelegramTransport(Transport)`: long-poll `getUpdates`
  (offset tracking, 429/5xx/network backoff), `sendMessage`/`answerCallbackQuery`/
  `editMessageText`. No new deps (httpx was already in-tree). The `Transport` ABC's
  first real second implementation.
- **`ui/handlers/telegram_bridge_handler.py`** — the bridge handler: state machine
  (disconnected/connecting/connected/error), inbound routing to the Supervisor,
  exec approvals via inline buttons, the app→phone reply mirror, and the remote
  command allowlist.
- **`utils/telegram_store.py`** — token + paired chat_id, atomic write, `0600` file /
  `0700` dir, token redacted from every log line.
- **`ui/views/settings_dialog.py`** — sectioned Notebook (Providers | Telegram Bridge)
  with token field (masked + reveal), Test, pairing flow, tooltips.
- **`ui/toolbar.py` / `ui/window.py`** — the Connect button is now REAL: a bridge
  toggle with honest states in all four conditions (unconfigured/off/connecting/
  connected); stop-all (either surface) drops the bridge.

## Trust posture (binding, honored)

One paired chat_id; the phone is a thin client — conversation, status, approvals,
unblock, stop-all. NO shell, NO file reads, NO review UI. A foreign chat gets exactly
one polite refusal (to the FOREIGN chat) and is never processed. The bot token is the
credential and never appears in logs.

## The audit cycle (the interesting part)

Debugger's adversarial pass found real defects the 100+-test suite did not:

1. **Transport stacking (bug).** `_on_transport_error` set ERROR but never stopped the
   transport, and the transport keeps polling after a soft error; `start_bridge` built a
   fresh transport with no teardown ⇒ TWO live poll loops ⇒ the Supervisor received
   every phone message twice. Fixed with a `stop_bridge()`-first teardown.
2. **Stale `/stop` (bug).** An armed confirmation survived ERROR→reconnect, so a later
   bare `STOP` fired stop-all on a "later session" — exactly the class §SP4 forbids.
3. **Foreign-refusal misdelivery (spec divergence).** `_send_refusal(chat_id)` ignored
   its argument; `send_message` always targeted the PAIRED chat, so the PM saw the
   refusal and the stranger got nothing. Fixed with a `chat_id` override.
4. **Round-2 regressions (introduced by the fixes!).** The teardown-first fix queued a
   stale `on_disconnect` on the deferred `GLib.idle_add` queue that clobbered state
   after the new connect (failed reconnect ended `disconnected` instead of `error`).
   The attribute-aware tag-strip fix was O(N²) on `<`-dense input on the GTK main
   thread. Both fixed with a session **generation** guard and a **linear** `HTMLParser`
   text extractor.
5. **`on_update` residual.** The same duplicate-delivery class, one line: the
   generation guard did not cover `on_update`. Guarded before close.

## Lessons

- **The tests were not the problem — the harness was.** Every gating bug was invisible
  to a green suite because the tests injected INLINE dispatch (`lambda fn,*a: fn(*a)`),
  which hides callback ordering. Production uses deferred `GLib.idle_add`. A
  `DeferredDispatch` harness (queue + explicit `drain()`) was required to see the
  stale-callback races. **Rule: when production defers execution, the test harness must
  defer too.**
- **A fix round is a changeset and needs its own audit.** Two of the round-1 fixes
  introduced regressions. Re-auditing the fixes caught them.
- **Prefer linear stdlib parsing over clever regexes** on any path that can run on the
  GTK main thread with semi-trusted input. The "smarter" tag regex was ~30× slower and
  quadratic.
- **The regex `mock` blind spot:** `FakeTransport.send_message` recorded text but not
  the target chat, so the foreign-refusal misdelivery was structurally invisible. Fakes
  must model the dimension the code is supposed to vary.

## Registered follow-ups (NOT in SPEC-15)

- **Blocking sends on the GTK main thread (issue).** Every outbound Telegram send runs
  synchronously on the main thread (up to ~60 s client timeout on a hung API). Needs a
  worker-thread/async design. Its own unit.
- **`ui/window.py:751` dead chat_handler response slot (suggestion).** Both paths that
  reach it are dead post-SPEC-05; harmless today, a latent trap. Cleanup candidate.
