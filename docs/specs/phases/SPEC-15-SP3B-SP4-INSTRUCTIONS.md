# SPEC-15 SP3b (finish) + SP4 — Phase Instructions

**Spec:** `docs/specs/SPEC-15-TELEGRAM-REMOTE-BRIDGE.md` (§SP3 tail + §SP4) — read it
in full FIRST; this file is the phase plan, the spec is the contract.
**Status entering this phase:** SP1 + SP2 DELIVERED; SP3a (approvals) + the SP3b
`forward_to_phone` BODY are already in the tree and committed (`c7cbb1cd`). What
remains: SP3b's TRIGGER (it is never called) and all of SP4.

Word marker for this delegation: **please write**.

---

## 0. Baseline (record verbatim)

```bash
.venv/bin/python -m pytest tests/test_telegram_bridge_handler.py tests/test_telegram_store.py tests/test_telegram_transport.py -q
.venv/bin/python -m ruff check ui/handlers/telegram_bridge_handler.py ui/window.py
```

---

## Part A — SP3b: wire the app→phone reply mirror (currently DEAD)

`TelegramBridgeHandler.forward_to_phone(text)` exists (telegram_bridge_handler.py:448)
with the 4096-char paragraph split, but **nothing calls it** — verified: a repo grep
finds only the definition and a stub test. The phone never receives Supervisor replies.

### A1. Filter seam on the bridge (bridge file)

Add a method that mirrors ONLY Supervisor replies:

```python
def on_supervisor_reply(self, session_key: str, text: str) -> None:
    """SP3b: forward a Supervisor turn's reply to the paired chat. No-op for
    any other session_key (the bridge is a Supervisor thin client only)."""
    if session_key != SUPERVISOR_KEY:
        return
    self.forward_to_phone(text)
```

### A2. Trigger (window.py — composition root, NOT ARH, NOT a handler import)

ARH exposes a SINGLE `set_on_agent_response(cb)` slot (agent_runtime_handler.py:239),
already occupied by `agent_command_handler.on_agent_response` (window.py:747). Do NOT
steal the slot — COMPOSE. In `_build`, replace the line-747 registration with a small
window method that calls BOTH, e.g.:

```python
def _on_agent_response(self, session_key, text, project_name):
    self._agent_command_handler.on_agent_response(session_key, text, project_name)
    bridge = getattr(self, "_bridge_handler", None)
    if bridge is not None:
        bridge.on_supervisor_reply(session_key, text)
```
and register `self._on_agent_response` at line 747. Resolve `_bridge_handler` LAZILY
(getattr) — it is constructed after the ARH in `_build`, and this callback only fires
at turn completion. A bridge exception must NEVER break the agent-response pipeline:
wrap the bridge call in try/except BLE001 (log via `logger.exception`).

### A3. HTML-card edge case (spec §5 last row)

A Supervisor reply can itself be one ```html fenced block (SPEC-13). Telegram is a
text surface — send the card's TEXT content, not markup. Add a small pure helper
(e.g. `_telegram_text(text)`) that, when the whole message is an html fence, strips
tags to plain text, else returns the text unchanged. Keep it dependency-light
(`re.sub` tag strip is acceptable; there is no existing html→text util — verified).
A plain-text reply passes through untouched.

### A4. Tests (RED-first; extend tests/test_telegram_bridge_handler.py)

- `on_supervisor_reply` forwards when session_key == "special:supervisor"; NO-OP for
  any other key (e.g. "special:coder") — assert `sent` stays empty.
- 4096-char split: a >4096 single-paragraph reply → multiple `send_message` calls, each
  ≤4096; a multi-paragraph reply splits on `\n\n` when a boundary exists after 1000.
- HTML-fence card → sent chunk contains the visible text and NO `<` tags.
- Empty/non-str text → no send, no raise.
- Replace the stale `test_forward_to_phone_stub_registered` (it only asserts non-raise)
  with the real behavioral battery above.

---

## Part B — SP4: stop-all, allowlist, docs

### B1. Remote command allowlist (bridge file, `_handle_message`)

Today every paired-chat text is forwarded verbatim to the Supervisor. Add a command
branch BEFORE the forward: when `text` starts with "/", handle ONLY the allowlist,
else one polite refusal listing it (spec §5: "refusal listing the allowlist").

Allowlist: `/status`, `/stop`, and (optionally) `/help`. Free chat text (no leading "/")
still forwards to the Supervisor. Unknown `/command` → refusal text, NOT forwarded.

### B2. `/status` (bridge → phone)

Return a compact, phone-readable status: active project name + work-unit buckets
(pending/active/blocked/done). Preferred source is the existing project status path —
`project_handler.cmd_status` needs a `Command` with a `project:`-prefixed session_key,
so if that is awkward from the bridge, build a small summary from the same data the
handler reads (do NOT invent a second status model). Keep it ≤ ~10 lines. If no active
project, say so honestly.

### B3. `/stop` — remote stop-all with TYPED confirmation (spec §SP4)

Destructive from the phone → require a typed confirmation word (e.g. the PM replies
`/stop confirm` or a follow-up exact word). Two-step: `/stop` → prompt with the exact
word; the exact word on the NEXT message → run stop-all. Any other reply aborts.

REUSE the existing stop-all path — do NOT reimplement it. Window's
`_on_stop_all_clicked` (window.py:1152) already (a) confirms, (b) calls
`arh.stop_all_agents()`, (c) calls `bridge.stop_bridge()`. The bridge must not import
window/ARH directly (handler isolation). Inject a callback: add
`set_stop_all_handler(cb)` on the bridge (house setter-injection pattern), where `cb`
is a window method that runs `stop_all_agents()` + `stop_bridge()` WITHOUT the GTK
confirm dialog (the phone already confirmed by typing the word). Wire it in `_build`
next to `set_feed_handler`.

### B4. Disconnect hygiene

The pending-stop confirmation must be dropped on `stop_bridge`/transport disconnect
(a stale confirmation must never fire on a later session) — same discipline as
`_clear_approval_msgs`.

### B5. Docs (all four, per spec §6 + §SP4)

- `docs/ARCHITECTURE.md` — add `transport/telegram.py` (long-poll HTTP; the ABC's
  first real second implementation) and a TelegramBridgeHandler module entry; note the
  Connect button is now real (the SPEC-05 "honest stub" note is superseded).
- `docs/specs/SPEC-05-R1-GATEWAY-STRIP.md` §8 — status note: the Telegram register is
  CLOSED by SPEC-15.
- `README.md` — a "remote-in" bullet: Connect = bridge the Supervisor to your phone
  (chat + exec approvals), thin client, no shell/file access.
- Toolbar tooltip (`ui/toolbar.py:50`) — the current text says "Telegram arrives
  post-MVP"; update to the real behavior.

### B6. Tests

- Allowlist: `/status` handled (no forward); `/stop` prompts (no forward, no stop
  yet); exact confirm word → `stop_all` callback fired exactly once; wrong word →
  aborted, callback NOT fired; unknown `/foo` → refusal, not forwarded; plain text →
  forwarded.
- Disconnect clears a pending confirmation.
- Toolbar/tooltip + docs presence (source-shape guard is acceptable for docs).

---

## Verification (paste ALL outputs)

```bash
.venv/bin/python -m pytest tests/test_telegram_bridge_handler.py tests/test_telegram_store.py tests/test_telegram_transport.py tests/test_window_telegram_bridge.py tests/test_settings_telegram_section.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_feed_handler.py -q
.venv/bin/python -m ruff check ui/handlers/telegram_bridge_handler.py ui/window.py ui/toolbar.py tests/test_telegram_bridge_handler.py tests/test_window_telegram_bridge.py
.venv/bin/python -m pyright ui/handlers/telegram_bridge_handler.py ui/window.py 2>&1 | tail -3
```

NO NEW ruff findings vs baseline. Redaction: any log line touching callback_data,
command text, or the token keeps the `redact_log_preview` pattern.

## Report format (mandatory)

Files changed + line numbers, baseline vs after counts, all command outputs verbatim,
COMPLETENESS checklist (one line per §A/§B item), related-bug scan (flagged, not fixed).

## Notes on this phase's seams (verified 2026-10-07)

- `forward_to_phone`: telegram_bridge_handler.py:448 (defined, NEVER called).
- ARH single response slot: agent_runtime_handler.py:239 (`set_on_agent_response`);
  registered at window.py:747.
- Supervisor completion dispatch: `_do_response_complete` fires `_on_agent_response`
  at agent_runtime_handler.py:2657.
- Stop-all path: window.py:1152 `_on_stop_all_clicked` → `arh.stop_all_agents()`
  (agent_runtime_handler.py:381) + `bridge.stop_bridge()`.
- Settings Telegram section already delivered (SPEC-15 SP2) — do NOT rebuild it.
- Bridge file header currently claims SP3a/SP3b — keep it accurate after this phase.
