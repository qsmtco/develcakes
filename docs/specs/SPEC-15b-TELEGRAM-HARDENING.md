# SPEC-15b: Telegram Bridge Hardening — Token-Log Redaction + 409 Conflict Surfacing

**Date:** 2026-10-07
**Author:** Supervisor
**Status:** READY
**Origin:** Real-user session 2026-10-07 — PM paired a bot; the transport token appeared in
plaintext in terminal logs, and a getUpdates conflict would have been invisible.
**Depends on:** SPEC-15 (Telegram Remote Bridge — DONE)
**Target branch:** main

---

## 1. Overview

Two defects found in a live SPEC-15 session, both small, both independent of the bridge's
core routing (which works).

**Problem A — the token leaks to logs (security).** SPEC-15 §2 promises the bot token is
"never logged" and ships `redact_log_preview()`. That helper only scrubs **app-authored**
log strings. The token rides in the request URL PATH (`/bot<token>/method`); the `httpx`
and `httpcore` libraries log that URL themselves at INFO/DEBUG. With `DEBUG` set
(`main.py:16` → `logging.basicConfig(level=DEBUG)`), every poll prints the raw token:

```
httpx INFO HTTP Request: POST https://api.telegram.org/bot8741404818:AAF.../getUpdates "HTTP/1.1 200 OK"
```

Nothing in the tree suppresses or redacts those library loggers. The §2 guarantee is
false whenever `DEBUG` is on — a real credential-exposure path.

**Problem B — a getUpdates conflict is swallowed (diagnosability).** Telegram permits ONE
`getUpdates` consumer per bot; a second poller gets `409 Conflict` (and/or its connection
reset — the observed `RemoteProtocolError('Server disconnected …')`). `_get_updates`
(`transport/telegram.py:390`) models only 429 and ≥500; every other non-ok body — including
409 — falls into a silent `return None` → generic "soft failure — backing off" forever.
No error state, no feed card, no phone notice: a conflict presents as a permanent silent
"Connecting…".

## 2. Scope

**In:**
- A logging redaction/filter covering `httpx`/`httpcore` (and the transport's own URL lines).
- Explicit `409 Conflict` modeling in `_get_updates` with an honest, distinct surface.
- Tests (RED-first) + the docs touch-up.

**Out:**
- Changing the bridge's routing/approvals/allowlist (SPEC-15, working).
- The blocking-send-on-main-thread follow-up (separate registered unit).
- Pairing-flow redesign (only the minimal conflict-awareness, below).

---

## 3. The change

### 3.1 Token-log redaction (Problem A)

1. **Suppress the library noise at the source (primary):** in `main.py`, after
   `logging.basicConfig`, floor the HTTP libraries so they never emit request URLs at the
   app's default levels:
   ```python
   for _noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
       logging.getLogger(_noisy).setLevel(logging.WARNING)
   ```
   Rationale: the URL is the leak vector; these libraries carry no signal we need above
   WARNING. Place it next to the existing config so it applies to every entry point that
   calls `logging.basicConfig`.

2. **Defense in depth — a redacting filter (belt):** add a `logging.Filter` that rewrites
   `record.msg`/`record.args` through `redact_log_preview` (the existing helper already
   scrubs `/bot<token>` and `token:`/`bot_token:` forms) and attach it to the `httpx` and
   `httpcore` loggers. This way, even if a future edit raises their level (or a different
   transport logs a URL), the token is still scrubbed before any handler formats it.
   Put the filter helper where it can import `redact_log_preview` without a cycle —
   `transport/telegram.py` or a small `utils/log_redaction.py` (preferred: the latter,
   so `main.py` does not import a transport).
   Note the ordering hazard: a filter runs BEFORE formatting, but `record.args` may hold
   the URL — scrub both `record.msg` and each str in `record.args`.

3. **Do not** rely solely on the transport's own `_logger` calls — those are already
   redacted; the leak is the library's, which is why (1)+(2) target the library loggers.

### 3.2 409 Conflict modeling (Problem B)

In `_get_updates`, before the generic non-ok branch, detect the conflict explicitly:

- Telegram's confirmed-poll error is **HTTP 409** with body `{"ok":false,"error_code":409,
  "description":"Conflict: terminated by other getUpdates request…"}`. Also treat an
  `error_code == 409` in an `ok:false` body as the same case (covers a 200-wrapped error).
- Raise a dedicated internal exception (mirror `_RetryAfter`), e.g. `_Conflict`.
- In `_poll_loop`, catch `_Conflict` and surface it **distinctly and honestly**:
  - log at WARNING (redacted),
  - fire `on_error` with a message that names the cause, e.g.
    `"another getUpdates consumer is active for this bot (409 Conflict) — is another app or a stale pairing poller using this token?"`,
  - back off (bounded) and keep the loop alive (do NOT crash the thread).
- Result: the bridge's existing `on_error` → `_set_state(ERROR)` → toolbar "offline" +
  the SPEC-15 error feed card now carries a REAL reason instead of staying silent.

**Also (cheap, in-scope):** the pairing flow (`telegram_settings_controller`) starts its
own `getUpdates` poller. Ensure a clean teardown before the bridge starts is at least
*visible*: this unit does NOT redesign pairing, but the 409 message must name the
possible stale-pairing cause so the user can act.

### 3.3 Docs

- `README.md` / settings tooltip: no behavior change needed beyond what SPEC-15 landed;
  if the 409 message is user-visible, it is self-explanatory.
- Note the redaction in `transport/telegram.py`'s module header (it currently claims the
  token "must NEVER appear in logs" — make that TRUE by referencing the filter).

---

## 4. Acceptance criteria

- [ ] With `DEBUG` set, a simulated getUpdates cycle emits NO line containing the token
      (test: capture logs from a fake client; assert `"/bot" + token` never appears).
- [ ] The redacting filter scrubs a token embedded in `record.msg` AND in `record.args`.
- [ ] `_get_updates` raises `_Conflict` on HTTP 409 and on an `ok:false` body with
      `error_code == 409`.
- [ ] `_poll_loop` on `_Conflict`: fires `on_error` with a 409-naming message, keeps the
      loop alive, backs off (does not spin at full rate).
- [ ] Existing transport/bridge suites stay green; ruff 0 new; pyright 0.

## 5. Edge cases

| Case | Behavior |
|---|---|
| Token embedded only in `record.args`, not `record.msg` | Still scrubbed (filter handles both) |
| `redact_log_preview` raises (defensive) | Filter must swallow and pass the record unchanged (never drop logs) |
| 409 recurring forever | Bounded backoff; distinct error each cycle (or deduped — pick one, pin it) |
| 409 then recovery | Existing F1b re-announce latch already re-fires on_connect |
| Non-telegram transport URL with a token-ish path | Out of scope — filter is attached to httpx/httpcore only |

## 6. Instructions for the implementer

- Files in scope: `main.py`, `transport/telegram.py`, and either
  `utils/log_redaction.py` (new, small) or an equivalent filter home; plus tests
  (`tests/test_telegram_transport.py`, and a new/extended logging test).
- **RED-first** (steelFramedCodeWriter): prove each fix fails before it lands.
  - A capture-handler test that asserts the token is absent from emitted records with
    `DEBUG` on (must fail before the fix).
  - A transport test that a 409 response raises `_Conflict` and that `_poll_loop`
    surfaces it via `on_error` (must fail before the fix).
- Reuse `redact_log_preview` — do NOT invent a second redactor.
- Do NOT touch the bridge handler, the settings dialog, or the pairing controller beyond
  reading them for context.
- Full battery + ruff/pyright per the standard gate.
