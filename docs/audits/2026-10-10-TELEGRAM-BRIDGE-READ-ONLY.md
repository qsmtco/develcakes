# Investigation Report — Telegram bridge (READ-ONLY)

**Date:** 2026-10-10
**Author:** Grok 4.7
**Scope:** Connect button, long-poll transport, settings pairing, and every path that sends a message to the phone. Read-only. No code changed by this investigation; this report is the artifact.
**Status:** DIAGNOSTIC ONLY
**Files read:** `transport/telegram.py`, `ui/handlers/telegram_bridge_handler.py`, `ui/handlers/telegram_settings_controller.py`, `utils/telegram_store.py`, `ui/toolbar.py`, `ui/window.py` (Connect + reply mirror), `ui/handlers/feed_handler.py` (`add_card` / auto-approve), `ui/handlers/agent_runtime_handler.py` (`_do_approval_needed`, `approve_exec`), `agent/runtime.py` (`exec_command` gate).

---

## TL;DR — VERDICT

The bridge is already careful about auth, pairing, and a second poller. The phone is noisy because every shell command is copied to Telegram as an Approve/Deny bubble, and accepting it on the desk does not clear that bubble.

File-review Accept and Reject never go to Telegram. The flood is `exec_command` ("requests approval to run command").

Three changes, in the order to do them:

1. **Do not mirror `exec_command` to the phone by default.** The feed card in the app stays the accept surface. A settings checkbox, "Mirror run approvals to the phone," default unchecked, is the whole feature. Keep mirroring sensitive `write_file` / `edit_file` and live-page approvals.
2. **Never send a card the app is about to auto-approve.** Show mode still creates the card and the bridge sends it, then the next idle runs the command anyway.
3. **When the desk resolves a card that was mirrored, edit that Telegram message and drop the buttons.** The edit path already exists; only a phone tap calls it today.

Connect is honest, but the click runs the network call on the GTK thread (`getMe`, then a wait of up to 10 seconds). "Connecting…" cannot paint until that returns. Settings Test and Pairing already use a daemon thread. Connect should match them. Outbound `sendMessage` is the same class of stall: synchronous HTTP on the UI thread, once per approval.

Do not retune the long poll. Do not build a digest until the checkbox is on and a single agent is still flooding the phone.

---

## 1. What reaches the phone

Two outbound pipes:

- Approval cards: `FeedHandler.add_card` → `on_card_added` → `TelegramBridgeHandler.on_feed_card_added`.
- Supervisor chat: `window._on_agent_response` → `on_supervisor_reply` → `forward_to_phone`. Only `special:supervisor` is forwarded.

| Trigger | Phone sees | Keep? |
|---|---|---|
| `exec_command` (every shell) | "Approval requested — $ command" plus Approve / Deny | No. This is the flood. The desk card is enough. |
| Show-mode auto-accept | Same bubble, then the command runs anyway | Never send. The app already approved it. |
| Sensitive `write_file` / `edit_file`, live-page calls | Same Approve / Deny card, rare | Keep. These are worth a phone tap. |
| Desk Approve or Deny after a mirror | Nothing. Buttons stay live until the phone is tapped | Edit the existing message. Do not send a second one. |
| File-review Accept / Reject | Not sent. Allowlist requires `needs_approval` | Correct. Leave it. |
| Supervisor final reply | The whole turn, split at 4096 characters | Keep. That is the remote chat. |
| `/status`, `/stop`, `/help`, foreign chat | One short reply | Keep. Already small. |

Silent exec auto-accept never creates a card (`agent_runtime_handler._do_approval_needed` returns before `add_card`), so that mode is already quiet on the phone.

---

## 2. Why run-command accept shows up

`agent/runtime.py` gates `exec_command` before the tool runs (`_dispatch_approval`). Sensitive-path `write_file` and `edit_file` use the same gate. The handler builds a feed card:

- Title: `{agent} requests approval to run command`
- Body: `$ {command}`
- Metadata: `needs_approval: True`, `status: pending_approval`

`on_feed_card_added` sends a Telegram message only when both of those metadata flags are set. That is the right allowlist for "this card needs a human," and it is why review Accept/Reject stays off the phone. It is the wrong grain for "ping the phone," because every shell command is a human gate.

Show mode makes it worse. `add_card` calls `on_card_added` (the Telegram send) and then queues `_auto_approve_exec_card` on the next idle. The phone is notified for a command the app is about to approve by itself.

Desk resolution does not talk to the bridge. `approve_exec` pops the pending approval, sets card status to `approved` or `denied`, and updates the feed. The Telegram edit (`edit_message_text`, buttons removed) lives only in `_handle_callback_query`, which runs when the phone button is tapped. Until then the phone still shows a live Approve.

A later phone tap does not double-run the command: the pending entry is already gone, the card re-read says approved or denied, and the message is edited to that. The damage is the notification and the stale button, not a second execution.

---

## 3. How to cut it

One policy change. The mirror already filters. Narrow the filter.

1. **Default off for run commands.** In `on_feed_card_added`, skip when `metadata.tool_name == "exec_command"` unless a stored flag says to mirror them. Sensitive writes and live-page cards (`live_bridge_call_id` / non-`exec_command` tool names) keep the current send.
2. **Skip auto-approve.** If exec auto-accept mode is `show` or `silent`, do not call `sendMessage`. Silent already never gets here; show is the hole.
3. **Desk click reuses the edit.** After `approve_exec` updates the card, if `_approval_msgs` has that card id, call the same edit the phone tap uses (`✅ Approved` / `❌ Denied`) and drop the inline keyboard. No second bubble.

A digest ("3 commands waiting") is more machinery than a default-off checkbox. Add it only if the checkbox is on and one agent still floods the chat.

---

## 4. Connect button

The toggle logic is sound (`ui/window.py` `_on_connect_clicked`):

- Not configured → gray "No transport (configure in Settings)", no poller.
- Configured and idle → `start_bridge()`.
- Connected or connecting → `stop_bridge()`.
- Supervisor not registered → refuse before any poll loop, with a specific feed card, not a generic "bad token" card.
- Pairing is a separate settings flow and requires a confirm before `chat_id` is saved.

The defect is where the network runs. `start_bridge` calls `asyncio.run(self._transport.connect())` on the GTK thread. `connect()` does `getMe`, then waits up to `CONNECT_TIMEOUT_SEC` (10s) for the poll thread. `stop_bridge` joins that thread for up to 2 seconds on the same thread. State changes go through `GLib.idle_add`, so "Connecting…" cannot paint until `connect()` returns and the main loop runs again. The window just freezes on "Connect".

`TelegramSettingsController.test_token` and `start_pairing` already run on daemon threads and dispatch the result back. Connect and disconnect should do the same.

There is a second toolbar writer. `Toolbar.update_connection_state` and `set_telegram_bridge_state` both set the same button label and status markup. Window startup calls `update_connection_state("offline")` and then `_init_bridge_toolbar_state()`, which overwrites it. Nothing else calls the legacy method. One function is enough; calling the old one later would clobber the Telegram label.

Pairing and the live bridge both long-poll `getUpdates`. Telegram allows one consumer per bot. Connecting while Start Pairing is still open is a 409. The poll loop surfaces that and the button goes to Offline. That handling is correct. The small fix is to refuse Connect, or stop the pairing poller, before opening a second one.

---

## 5. Efficiency

Leave the poll loop alone. It is a 50-second long poll, `allowed_updates` limited to `message` and `callback_query`, offset advanced only after the update is handled, backoff on 429 (honoring `retry_after`) and 5xx, and the bot token scrubbed out of log lines. That is the right shape.

Sends are the cost. `send_message`, `edit_message_text`, and `answer_callback_query` are synchronous `httpx` posts. They run on the GTK thread because the card callback and the inbound update handler are both dispatched there with `idle_add`. One slow Telegram POST stalls the window, and a chatty agent multiplies that by the number of shell commands. Moving those three calls off the main thread is the efficiency win. Cutting the `exec_command` mirror removes most of the calls.

The HTML flattener for Supervisor cards (`_html_fragment_to_text`) is linear and fail-safe. It is not the thing to simplify.

---

## 6. What to leave alone

These are earned and should stay:

- Session generation so a torn-down poller's queued callbacks cannot clobber the new session.
- Foreign-chat refusal (one polite reply to the stranger, nothing routed).
- Token redaction (`redact_log_preview`, plus the httpx log filter).
- Fail-closed `getMe` before the poll loop starts.
- Typed `/stop STOP` confirm. Stop-all is not a single tap.
- The `needs_approval` allowlist itself. Narrow it; do not delete it.

The handler is long because it owns routing, approvals, and stop. Splitting the HTML helper out does not make the phone quieter.

---

## 7. Suggested order if this is implemented later

1. Skip `exec_command` in `on_feed_card_added` unless a new store flag is on. Default off.
2. Skip the send when exec auto-accept is `show`.
3. After desk `approve_exec`, edit the mirrored Telegram message if one exists.
4. Run `start_bridge` / `stop_bridge` connect and disconnect on a daemon thread, same as pairing.
5. Run `send_message` / `edit_message_text` / `answer_callback_query` off the GTK thread.
6. Delete `Toolbar.update_connection_state` (and the startup call) once nothing references it.
7. If pairing is active, stop it or refuse Connect before `start_bridge`.
