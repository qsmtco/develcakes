# SPEC-15: Telegram Remote Bridge — Chat with the Supervisor From Your Phone

**Date:** 2026-10-07
**Author:** Supervisor (develcakes v2)
**Status:** IN PROGRESS — SP1 DELIVERED + RE-AUDITED PASS (transport/telegram.py + 27-test battery; client-close, dispatch-guard, idempotent-disconnect hardened post-audit; REGISTER BUG#5 deferred to SP2/SP3 by design). SP2 (bridge handler + settings + Connect wiring) next.
**Implements:** PM direction 2026-10-07 — "Connect button = a Telegram bridge session
to the Supervisor. Click Connect → continue working remotely; click Disconnect →
back at the desk." First post-MVP unit. PM has a bot token in hand.
**Depends on:** SPEC-05 (Transport ABC + cleaned connection core — decision #1
pre-ruled reuse), SPEC-12/13/14 (the chat surface the bridge mirrors into)
**Target branch:** main

---

## 1. Overview

**Problem.** develcakes is local-first; the human must be at the desk for the loop
to get decisions. The Supervisor (the PM's proxy in the agent trio) stalls on
approvals/unblocks/priority calls the moment the human leaves. The Connect button
exists as an honest stub (SPEC-05 SP3c) with the `Transport` ABC + cleaned
connection core waiting unused since 2026-09-23.

**Solution.** A **Telegram bridge** — the phone is a thin client for the
Supervisor. Connect = bridge session up (button → Disconnect, green status);
the PM's Telegram chat mirrors the project group chat with the Supervisor as the
counterparty. Supervisor replies come back as Telegram messages rendered through
the SAME sanitizer pipeline as in-app cards. Exec approvals surface as Telegram
inline buttons (the thing that actually blocks the loop while away).

**Trust posture (binding):** the phone is a thin client — conversation, status,
approvals, unblock, stop-all. NO remote shell, NO file reads, NO code review UI.
The bot token is the credential (stored with provider secrets); pairing binds
exactly ONE chat id; a foreign chat gets one polite refusal, nothing more.
Stop-all drops the bridge.

**Scope**

| In | Out |
|---|---|
| `TelegramTransport` (long-polling, no new deps — httpx is in-tree) | Group chats (one bot ↔ one paired chat) |
| Settings → "Telegram Bridge" section (token + Test + pairing) | Sectioned-dialog refactor beyond the new section |
| Connect/Disconnect wiring on the toolbar button | Remote file review / diffs / agent config |
| Message mirror: PM↔Supervisor both directions | Broadcast to all agents (post-MVP+) |
| Exec approvals via inline buttons (allowlist) | Remote shell / file access (NEVER) |
| Stop-all integration (drop bridge) | Multi-project routing (active project only) |
| Docs + tests | Inline-button editing of arbitrary agent state |

## 2. Architecture

```
Phone (Telegram) ⇄ TelegramTransport (long-poll 2.40s HTTP getUpdates via httpx)
                        │  pairs ONE chat_id
                        ▼
               TelegramBridgeHandler (new, ui/handlers/)
                        │  routes PM text → ARH.send_to_special_agent(supervisor, text)
                        │  routes Supervisor replies → transport.send (Telegram)
                        ▼
              existing runtime/ARH — no new agent path
```

- **Transport layer** (`transport/telegram.py`): `TelegramTransport(Transport)`.
  Long-polling `getUpdates` (allowed_updates: message, callback_query), plain
  `sendMessage` (MarkdownV2 OFF — plain text; HTML rendering is OUR surface's
  job, not Telegram's), `answerCallbackQuery` + `editMessageText` for approvals.
  Reuses the OpenClaw-cleaned patterns (backoff, redaction, signal-thread
  discipline) but NOT its WebSocket internals — Telegram is HTTP.
- **Bridge handler** (`ui/handlers/telegram_bridge_handler.py`): owns session
  state (disconnected/connecting/connected/error), routes messages, enforces the
  allowlist, marshals everything to the GTK main thread (existing `_dispatch`
  pattern), and is the ONLY writer of bridge state (single-writer rule).
- **Settings section** (`ui/views/settings_dialog.py`): grow the dialog a
  sectioned layout (Notebook: Providers | Telegram Bridge). Token field
  (masked + reveal, clone of `_reveal_btn`), **Test** button (httpx getMe
  off-thread, dispatch result — clone of `test_provider` pattern), pairing
  flow (start → poll for the PM's `/start` from Telegram → bind chat_id →
  confirm), unpair.
- **Toolbar** (`ui/window.py` wiring): `_on_connect_clicked` stops being a
  no-op — becomes bridge toggle. Status label gains a connected state
  (● Telegram bridge, green). Honesty-tier fix folds in here (real button at
  last).
- **Persistence**: token + paired chat_id in the config dir
  (`telegram_bridge.yaml`), 0600 perms — same dir/perm discipline as provider
  secrets. Never logged (redact in any debug output — `redact_log_preview`
  pattern).

## 3. Phases

### SP1 — TelegramTransport (pure, no UI)
`transport/telegram.py`: connect (validate token via getMe), long-poll loop
(getUpdates, offset tracking, backoff on 429/5xx/network per OpenClaw patterns),
send (sendMessage / answerCallbackQuery / editMessageText), `status_signals()`
compliance, is_connected. Fail-closed: no token → connect raises clean error;
malformed update → skip + log. Poll-driven testability: transport takes a
`get_updates`/`api_call` injectable so tests run without network (httpx client
injected; tests use a fake).
Tests: fake-API battery — connect/getMe fail-closed, poll loop + offset advance,
backoff on 429/5xx, send shapes, redaction of token in logs, disconnect idempotent.

### SP2 — Bridge handler + Connect/Disconnect + settings section
- `TelegramBridgeHandler`: state machine + routing + allowlist + main-thread
  marshal. PM text → `send_to_special_agent("special:supervisor", text)`.
  Supervisor completion → bridge forwards the reply text to Telegram.
- Settings: sectioned dialog (Notebook), token+Test+pair/unpair UI,
  `utils/telegram_store.py` (0600, redacted logging).
- Window: Connect button → bridge toggle; status label states; honest disabled
  state when unconfigured ("Connect (not configured)" + tooltip).
Tests: state machine matrix, routing both directions (fake transport), allowlist
refusals, pairing flow, store 0600 + redaction, button wiring, unconfigured
honesty state.

### SP3 — Exec approvals via Telegram (the loop unblocker)
- Bridge subscribes to pending-approval events: when ARH emits an approval card,
  bridge sends an inline-button message (✅ Approve / ❌ Deny).
- callback_query → validate chat_id + button payload → `ARH.approve_exec` →
  edit the Telegram message to the resolved state + feed card reflects it
  (same card, both surfaces).
- Race rules: approval resolved at the desk first → Telegram edit says "already
  resolved in app"; stale/duplicate callback → answer with a no-op notice.
Tests: approve/deny e2e on fake transport, desk-first race, stale callback,
foreign chat_id rejection, allowlist (only exec approvals surface — never
auto-accepted writes).

### SP4 — Stop-all integration + hardening + docs
- Stop All drops the bridge (toolbar path AND remote command) + confirmation
  asymmetry (stop-all from phone: requires typed confirmation word, it's
  destructive).
- Remote allowlist final: /status, /stop, chat text → Supervisor, approve/deny
  buttons. Everything else → polite refusal.
- Docs: ARCHITECTURE (transport/ gains telegram.py; bridge module entry),
  SPEC-05 status note (register closed by SPEC-15), README (remote-in bullet),
  settings tooltips. Context.md entry.
- Full battery + post-mortem.

## 4. Acceptance criteria

- [ ] Connect with no token → honest state change, no crash, feed card
- [ ] Token + pairing via Settings; token never in logs; file 0600
- [ ] Connect → Telegram PM↔Supervisor conversation works both directions
- [ ] Supervisor replies render in-app via the SPEC-13/14 pipeline (chrome intact)
- [ ] Exec approval appears on the phone as buttons; tapping approves/denies;
      in-app card resolves identically; desk-first race handled
- [ ] Foreign chat id: one refusal, never processed, logged redacted
- [ ] Stop All (either surface) drops the bridge; button returns to Connect
- [ ] Disconnect → clean teardown, no orphaned poll loop
- [ ] Toolbar honest in all four states (unconfigured/off/connecting/connected)
- [ ] ruff 0 new, pyright 0, full suite green; RED-first tests + kill-proofs

## 5. Edge cases

| Case | Behavior |
|---|---|
| Telegram API down mid-session | backoff reconnect; status label → connecting; UI stays live |
| Two Connect clicks | state machine no-ops the second |
| Token revoked server-side | 401 on next poll → error state + feed card, button resets |
| PM sends message while Supervisor turn in flight | queued by existing runtime turn machinery (no new queue) |
| Supervisor reply > 4096 chars (Telegram msg cap) | split on paragraph boundaries, ≤4096 each |
| Pairing code timeout | expire after 5 min, retry |
| App quit with bridge up | disconnect in shutdown path (window close hook) |
| Bot added to a GROUP chat | refusal + instruction to use the private chat (security) |
| `/command` from phone that isn't allowlisted | refusal listing the allowlist |
| Long supervisor reply containing an HTML card | send the card's TEXT content (sanitized strip), not markup — Telegram is a text surface |

## 6. ARCHITECTURE.md updates required

- §transport/ — add `telegram.py` (long-poll HTTP; the ABC's first real second
  implementation); Connect button now real.
- §Modules — TelegramBridgeHandler entry (handler graph).
- §Data Flow — remote-in path: Telegram ⇄ bridge ⇄ ARH (PM↔Supervisor only).
- §Patterns — thin-client trust boundary (phone = conversation + approvals).
- §Error Handling — bridge failure modes (poll death, revoked token).
